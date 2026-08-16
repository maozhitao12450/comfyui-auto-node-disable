"""
auto_disable.py
================
核心模块：追踪 ComfyUI custom_nodes 中各节点模块的使用情况，
并在连续若干次『打开页面（即入队 prompt）』未被使用时，
将其整体移动到 ``ComfyUI/custom_nodes/.disabled/<原名>/`` 子目录中禁用。

设计要点
--------
- 通过遍历全局 ``NODE_CLASS_MAPPINGS``，利用每个节点类上的
  ``RELATIVE_PYTHON_MODULE`` 属性，反向构建
  ``custom_node 子目录名 -> 它提供的节点类名集合`` 的映射。
- 每次有工作流入队（``prompt`` API），都从 prompt 中提取本次用到的节点类
  并写入一份滚动状态文件（默认放在 ComfyUI 根目录下 ``auto_node_disable_state.json``）。
- 当滚动窗口内连续 ``threshold``（默认 3）次入队都未出现某 custom_node 的节点类时，
  将该 custom_node 目录移动到 ``custom_nodes/.disabled/<原名>/`` 下。
- 用户可在状态文件里通过 ``exclude`` 列表永久保留某些 custom_node 不被自动禁用。

反向能力：自动恢复
------------------
当用户提交的工作流引用了**当前未注册的节点类**（多半是被自动禁用导致），
会去 ``.disabled/`` 里寻找能提供这些类名的模块并自动移回
``custom_nodes/``，同时把"待重启"条目写入 ``state["pending_restart"]``，
由前端在下次 ``/auto_disable/status`` 拉取时弹出"请重启 ComfyUI"提示。

安全边界
--------
为防止"目录已移动但状态未落盘"或"重复入队造成误禁用"造成的不可恢复副作用，
热路径引入以下最小边界：

1. **干跑 (dry-run)**：状态里写入 ``dry_run: true`` 后，禁用动作只更新审计字段、
   不实际移动目录，便于先观察再放开。
2. **入队关联标识**：每次入队的 ``prompt_id`` 透传到 ``rounds`` 条目；
   触发禁用的模块会把这次 ``prompt_id`` 一并写入 ``disabled`` 条目，便于事后
   重建"哪个入队导致哪个目录被禁用"的因果链。
3. **原子化移动 + 回滚**：禁用采用"先持久化 pending 状态 → 再移动 → 最后
   落 confirmed 状态"的顺序；若移动失败则从状态里回滚该模块，避免出现
   "目录已搬走但 disabled 字典里没有它"的孤儿状态；若进程在中间崩溃，
   下次启动会按文件存在与否自动对齐状态与目录。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from typing import Any, Iterable, Optional

log = logging.getLogger("auto_node_disable")


# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------

# 状态文件名（保存到 ComfyUI 根目录，即 main.py 所在目录的上一层）
STATE_FILENAME = "auto_node_disable_state.json"

# 禁用目录名（位于 custom_nodes/ 下）
DISABLED_DIR_NAME = ".disabled"

# 每次『打开页面』= 一次 prompt 入队；阈值默认 30 次
DEFAULT_THRESHOLD = 30

# 默认排除列表：永不自动禁用的 custom_node 子目录名
DEFAULT_EXCLUDE = (
    "comfyui-auto-node-disable",
    "ComfyUI-Manager",
)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _custom_nodes_dir() -> str:
    """定位 ComfyUI 根目录下的 ``custom_nodes`` 目录。

    ComfyUI 通常把 ``custom_nodes`` 放在仓库根目录。
    我们通过 ``nodes.py`` 同级目录的兄弟目录 ``custom_nodes`` 解析；
    若不可用，则退回到环境变量 ``COMFYUI_PATH`` 拼接。
    """
    try:
        import nodes  # ComfyUI 提供的全局模块
        comfy_root = os.path.dirname(os.path.abspath(nodes.__file__))
    except Exception:
        comfy_root = os.environ.get("COMFYUI_PATH", os.getcwd())

    return os.path.join(comfy_root, "custom_nodes")


def _comfy_root() -> str:
    """ComfyUI 根目录（``custom_nodes`` 的父目录）。"""
    return os.path.dirname(_custom_nodes_dir())


def _state_path() -> str:
    return os.path.join(_comfy_root(), STATE_FILENAME)


def _disabled_dir() -> str:
    return os.path.join(_custom_nodes_dir(), DISABLED_DIR_NAME)


# ---------------------------------------------------------------------------
# 状态管理（线程安全 + 原子写）
# ---------------------------------------------------------------------------

_state_lock = threading.RLock()


def _load_state() -> dict[str, Any]:
    """读取状态文件；不存在则返回默认结构。"""
    path = _state_path()
    if not os.path.exists(path):
        return _default_state()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.warning("auto_node_disable: state file unreadable (%s); starting fresh", e)
        return _default_state()
    # 兼容旧结构：补齐缺失字段
    defaults = _default_state()
    for k, v in defaults.items():
        data.setdefault(k, v)
    # pending_restart 必须是 list；旧版本不存在该字段时已被 setdefault 填充为 []
    if not isinstance(data.get("pending_restart"), list):
        data["pending_restart"] = []
    # 启动时对齐 pending 与目录实际位置（进程崩溃后恢复用）
    pending_changed = _reconcile_pending(data)
    # 启动时对齐 disabled 与 .disabled/ 磁盘：补齐手工禁用、清理被手动恢复的条目
    disk_result = _reconcile_disabled_with_disk(data)
    if pending_changed or disk_result["changed"]:
        try:
            _atomic_write_json(path, data)
        except Exception as e:
            log.warning(
                "auto_node_disable: failed to persist reconciled state: %s", e
            )
        if disk_result["added"] or disk_result["restored"]:
            log.info(
                "auto_node_disable: reconcile on startup: added=%s, restored=%s, "
                "warnings=%s",
                disk_result["added"],
                disk_result["restored"],
                disk_result["warnings"],
            )
    return data


def _reconcile_disabled_with_disk(state: dict[str, Any]) -> dict[str, Any]:
    """把 ``state["disabled"]`` 与 ``.disabled/`` 目录做对账（启动时调用）。

    四种场景：

    1. state 有、磁盘有：正常，保留；顺便把孤悬的 ``pending`` 标记为
       ``confirmed``（不依赖 ``_reconcile_pending``，保证对账后状态收敛）。
    2. state 有、磁盘无、且 ``original_path`` 重新可见：视为被用户
       手动恢复，从 state 里删除。
    3. state 有、磁盘无、且 ``original_path`` 也看不到：告警并保留
       （不擅自删除，避免误操作）。
    4. state 无、磁盘有：当作"被外部禁用"，追加进 state，状态
       ``confirmed``，``original_path`` 留空（无法可靠反查原始路径）。

    返回 ``{"changed": bool, "added": [...], "restored": [...],
    "warnings": [...]}``，调用方可据此决定是否需要持久化。
    """
    disabled = state.get("disabled")
    if not isinstance(disabled, dict):
        return {"changed": False, "added": [], "restored": [], "warnings": []}

    changed = False
    ddir = _disabled_dir()
    disk_names: set[str] = set()
    if os.path.isdir(ddir):
        try:
            for entry in os.listdir(ddir):
                full = os.path.join(ddir, entry)
                if os.path.isdir(full) or os.path.isfile(full):
                    disk_names.add(entry)
        except Exception as e:
            log.warning(
                "auto_node_disable: reconcile: cannot list %s: %s", ddir, e,
            )

    added: list[str] = []
    restored: list[str] = []
    warnings: list[str] = []

    # Pass 1: state 已有条目 -> 对账磁盘
    for name, info in list(disabled.items()):
        if not isinstance(info, dict):
            warnings.append(name)
            continue
        on_disk = name in disk_names
        if on_disk:
            if info.get("status") == "pending":
                info["status"] = "confirmed"
                changed = True
            continue
        # 磁盘上没有 -> 检查是否被恢复回原位
        original = (info.get("original_path") or "").strip()
        if original and os.path.exists(original):
            disabled.pop(name, None)
            changed = True
            restored.append(name)
            log.info(
                "auto_node_disable: reconcile: removed %s from disabled "
                "(original_path %s exists again)",
                name, original,
            )
            continue
        # 既不在磁盘、也不在原位 -> 告警保留
        warnings.append(name)
        log.warning(
            "auto_node_disable: reconcile: %s is in state['disabled'] but "
            "neither on disk (.disabled/) nor at original_path %s; "
            "leaving as-is",
            name, original or "<empty>",
        )

    # Pass 2: 磁盘上有、state 没有 -> 补齐
    for name in sorted(disk_names):
        if name in disabled:
            continue
        disabled[name] = {
            "original_path": "",
            "disabled_at": time.time(),
            "status": "confirmed",
            "node_classes": [],
        }
        changed = True
        added.append(name)
        log.info(
            "auto_node_disable: reconcile: added %s to state['disabled'] "
            "(found in .disabled/ but missing from state)",
            name,
        )

    return {
        "changed": changed,
        "added": added,
        "restored": restored,
        "warnings": warnings,
    }


def _reconcile_pending(state: dict[str, Any]) -> bool:
    """对 ``disabled`` 里 ``status == "pending"`` 的条目按文件存在与否收敛。

    - 路径仍在原位 → 移动未发生，回滚记录（避免孤儿状态）；
    - 路径已不在 → 移动实际成功，标记 ``confirmed``。

    返回 ``True`` 表示状态被修改过。
    """
    disabled = state.get("disabled")
    if not isinstance(disabled, dict) or not disabled:
        return False
    changed = False
    for module_name, info in list(disabled.items()):
        if not isinstance(info, dict):
            continue
        if info.get("status") != "pending":
            continue
        original = info.get("original_path") or ""
        if not original:
            # 无路径信息无法对齐：直接当作已确认
            info["status"] = "confirmed"
            changed = True
            continue
        if os.path.exists(original):
            # 路径还在 → 移动未完成，回滚
            disabled.pop(module_name, None)
            changed = True
            log.warning(
                "auto_node_disable: rolled back stale pending record for %s (%s still exists)",
                module_name, original,
            )
        else:
            # 路径已不在 → 移动实际成功
            info["status"] = "confirmed"
            changed = True
            log.info(
                "auto_node_disable: confirmed pending disable for %s (path already moved)",
                module_name,
            )
    return changed


def _default_state() -> dict[str, Any]:
    return {
        "threshold": DEFAULT_THRESHOLD,
        "dry_run": False,
        "exclude": list(DEFAULT_EXCLUDE),
        "known_modules": {},   # module_name -> {"node_classes": [...], "module_path": "..."}
        "rounds": [],         # 滚动窗口：每条 {"timestamp": float, "used_classes": [...], "prompt_id"?: str}
        "disabled": {},       # module_name -> {"original_path": "...", "disabled_at": float,
                              #                     "prompt_id"?: str,
                              #                     "node_classes"?: [...],
                              #                     "status": "pending"|"confirmed"|"dry_run"}
        # 因提交工作流命中了 .disabled 里某模块而自动恢复时记录在这里，
        # 前端轮询 /auto_disable/status 消费后清空，提示用户重启 ComfyUI。
        # 每条: {"module": str, "node_classes": [...], "restored_at": float,
        #         "prompt_id"?: str}
        "pending_restart": [],
    }


def _atomic_write_json(path: str, data: dict[str, Any]) -> None:
    """原子写入 JSON，避免半写文件导致损坏。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _save_state(state: dict[str, Any]) -> None:
    try:
        _atomic_write_json(_state_path(), state)
    except Exception as e:
        log.warning("auto_node_disable: failed to persist state: %s", e)


# ---------------------------------------------------------------------------
# 反向映射：custom_node 子目录 -> 它提供的节点类集合
# ---------------------------------------------------------------------------

def _scan_known_modules(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """遍历全局 ``NODE_CLASS_MAPPINGS``，构建 ``custom_node 模块名 -> 节点类列表`` 映射。

    节点类 ``RELATIVE_PYTHON_MODULE`` 形如 ``"custom_nodes.<module_name>"``；
    直接取 ``.`` 后的最后一段即可得到 custom_node 子目录名。

    V3 扩展（``comfy_entrypoint`` 返回 ``ComfyExtension``）同样会把节点加入
    ``NODE_CLASS_MAPPINGS``，因此同样适用。
    """
    try:
        from nodes import NODE_CLASS_MAPPINGS  # ComfyUI 全局映射
    except Exception as e:
        log.warning("auto_node_disable: cannot import NODE_CLASS_MAPPINGS: %s", e)
        return state.setdefault("known_modules", {})

    found: dict[str, dict[str, set[str] | str]] = {}

    for class_name, node_cls in NODE_CLASS_MAPPINGS.items():
        rel = getattr(node_cls, "RELATIVE_PYTHON_MODULE", None)
        if not rel:
            # 内建节点（来自 ComfyUI 自身 / comfy_extras）通常不携带该属性
            continue
        # 形如 "custom_nodes.<module_name>" 或 "custom_nodes.<module_name>.<sub>"
        parts = rel.split(".")
        if len(parts) < 2 or parts[0] != "custom_nodes":
            continue
        module_name = parts[1]
        if not module_name:
            continue
        bucket = found.setdefault(
            module_name, {"node_classes": set(), "module_path": ""}
        )
        bucket["node_classes"].add(class_name)

    # 把集合转 list 并尝试定位物理路径
    result: dict[str, dict[str, Any]] = {}
    for module_name, info in found.items():
        module_path = _resolve_module_path(module_name)
        result[module_name] = {
            "node_classes": sorted(info["node_classes"]),
            "module_path": module_path,
        }

    state["known_modules"] = result
    return result


def _resolve_module_path(module_name: str) -> str:
    """根据模块名定位它在 ``custom_nodes`` 下的真实路径（目录或单文件）。"""
    base = _custom_nodes_dir()
    direct = os.path.join(base, module_name)
    if os.path.isdir(direct):
        return os.path.abspath(direct)
    file_py = os.path.join(base, module_name + ".py")
    if os.path.isfile(file_py):
        return os.path.abspath(file_py)
    return ""


# ---------------------------------------------------------------------------
# 记录一次『页面打开』事件（即一次 prompt 入队）
# ---------------------------------------------------------------------------

def record_prompt(
    prompt_or_json: Any, prompt_id: Optional[str] = None
) -> dict[str, Any]:
    """记录一次 prompt 入队事件，叠加到滚动窗口；并触发决策。

    :param prompt_or_json: 可以是 ComfyUI ``/prompt`` 请求体
        （``{"prompt": {...}, "client_id": ..., ...}``），也可以是已经提取好的
        节点类名可迭代对象。会通过 ``extract_used_class_names`` 统一转换。
    :param prompt_id: 可选的入队标识（如 ComfyUI 生成的 ``prompt_id``）。
        会被写入 ``rounds`` 条目，并在该轮触发禁用时进入 ``disabled`` 记录，
        用于事后审计"哪个入队导致哪个目录被禁用"。如果 ``prompt_or_json``
        是 dict 且包含 ``prompt_id`` 字段，则优先采用字段值。
    :return: 本次入队后的进度摘要，字段包括 ``rounds_count`` / ``threshold`` /
        ``keep`` / ``cap`` / ``dry_run`` / ``known_count`` /
        ``newly_disabled`` / ``disabled_count``，供调用方在日志/遥测里使用。
        - ``cap`` = ``keep * 4``，是滚动窗口的裁剪上限。
        - ``known_count`` 为已反推出的 ``custom_node`` 模块数。
        - ``newly_disabled`` 是本次决策新加入 ``disabled`` 的模块名列表。
        - ``disabled_count`` 是 ``disabled`` 字典的当前条目总数（含
          pending/confirmed/dry_run）。
    """
    # 兼容两种输入：dict (原始请求体) 或可迭代对象 (已提取的类名集合)
    if isinstance(prompt_or_json, dict):
        used_class_names = extract_used_class_names(prompt_or_json)
        if prompt_id is None:
            prompt_id = prompt_or_json.get("prompt_id")
    else:
        used_class_names = prompt_or_json or []

    # 规范化入队关联标识：仅保留非空字符串
    if prompt_id is not None:
        prompt_id = str(prompt_id) if str(prompt_id) else None

    with _state_lock:
        state = _load_state()
        # 每次启动/扫描都可能发现新模块，先刷新
        _scan_known_modules(state)

        used = sorted(set(used_class_names))

        # 1) 缺失节点类自动恢复：在写入本轮 round 之前尝试把已禁用的
        #    能提供本次所需类名的模块移回 custom_nodes/。
        #    若有命中，会把待重启条目写入 state["pending_restart"]，
        #    前端在 prompt 响应后再发起一次 /auto_disable/status 拉取即可拿到。
        try:
            restore_for_missing_classes(state, used, prompt_id=prompt_id)
        except Exception as e:
            log.warning("auto_node_disable: auto-restore step failed: %s", e)

        round_entry: dict[str, Any] = {"timestamp": time.time(), "used_classes": used}
        if prompt_id is not None:
            round_entry["prompt_id"] = prompt_id
        state["rounds"].append(round_entry)

        # 修剪滚动窗口大小：保留足够做决策的窗口
        keep = max(int(state.get("threshold", DEFAULT_THRESHOLD)) + 2, 5)
        if len(state["rounds"]) > keep * 4:
            state["rounds"] = state["rounds"][-keep * 4:]

        _save_state(state)

        # 即时决策（传入本次入队标识以便写入 disabled 审计字段）
        newly_disabled: list[str] = []
        try:
            newly_disabled = _decide(state, last_prompt_id=prompt_id) or []
        except Exception as e:
            log.warning("auto_node_disable: decision step failed: %s", e)
        else:
            _save_state(state)

        # 返回本次入队后的进度摘要，供调用方记录到日志/遥测。
        disabled_map = state.get("disabled") or {}
        return {
            "rounds_count": len(state["rounds"]),
            "threshold": int(state.get("threshold", DEFAULT_THRESHOLD)),
            "keep": keep,
            "cap": keep * 4,
            "dry_run": bool(state.get("dry_run", False)),
            "known_count": len(state.get("known_modules") or {}),
            "newly_disabled": list(newly_disabled),
            "disabled_count": len(disabled_map),
        }


# ---------------------------------------------------------------------------
# 决策：哪些 custom_node 应该被禁用
# ---------------------------------------------------------------------------

def _decide(
    state: dict[str, Any],
    last_prompt_id: Optional[str] = None,
) -> list[str]:
    """根据滚动窗口判定哪些 custom_node 模块在最近 N 轮都未被使用。

    返回新被禁用（或被记为干跑）的模块名列表。

    每个被处理的模块都会走三步安全边界：

    1. 若 ``state["dry_run"]`` 为真，只写审计字段、不移动目录；
    2. 否则先在 ``state["disabled"][m]`` 上写 ``status="pending"`` 并立即
       落盘（确保在目录被移动前禁用决策已经被记录）；
    3. 再调用 ``_disable_module`` 实际移动目录。
       - 移动成功：把状态改为 ``"confirmed"`` 并落盘；
       - 移动失败：从 ``disabled`` 中移除该模块并落盘，回滚到一致状态。
    """
    threshold = int(state.get("threshold", DEFAULT_THRESHOLD))
    if threshold <= 0:
        return []

    known: dict[str, dict[str, Any]] = state.get("known_modules", {})
    if not known:
        return []

    rounds: list[dict[str, Any]] = state.get("rounds", [])
    recent = rounds[-threshold:] if len(rounds) >= threshold else rounds
    if not recent:
        return []

    # 取最近 N 轮使用过的节点类并集
    used_union: set[str] = set()
    for r in recent:
        used_union.update(r.get("used_classes", []))

    exclude = set(state.get("exclude", []) or DEFAULT_EXCLUDE)
    disabled: dict[str, Any] = state.setdefault("disabled", {})
    dry_run = bool(state.get("dry_run", False))

    newly: list[str] = []
    for module_name, info in known.items():
        if module_name in exclude:
            continue
        if module_name in disabled:
            # 去重：已在 disabled 中（不管是 pending/confirmed/dry_run），跳过
            continue
        node_classes = set(info.get("node_classes", []))
        if not node_classes:
            continue
        # 若该模块的所有节点类在最近 N 轮里都未出现，则视为可禁用
        if not node_classes.isdisjoint(used_union):
            continue

        if dry_run:
            # 干跑：只写审计字段，不移动目录
            disabled[module_name] = {
                "original_path": info.get("module_path", ""),
                "disabled_at": time.time(),
                "prompt_id": last_prompt_id,
                "node_classes": sorted(node_classes),
                "status": "dry_run",
            }
            newly.append(module_name)
            log.info(
                "auto_node_disable: [DRY-RUN] would disable %s (prompt %s)",
                module_name, last_prompt_id,
            )
            continue

        # 1) 先持久化 pending 状态：保证“目录还未移动时禁用决策已落盘”
        pending_record: dict[str, Any] = {
            "original_path": info.get("module_path", ""),
            "disabled_at": time.time(),
            "prompt_id": last_prompt_id,
            "node_classes": sorted(node_classes),
            "status": "pending",
        }
        disabled[module_name] = pending_record
        # 直接调原子写，不走 _save_state 的吞异常路径，否则失败会被吃掉
        try:
            _atomic_write_json(_state_path(), state)
        except Exception as e:
            # 持久化失败：不进入移动步骤，以免出现“未记录的移动”
            disabled.pop(module_name, None)
            log.warning(
                "auto_node_disable: failed to persist pending disable for %s: %s",
                module_name, e,
            )
            continue

        # 2) 实际移动目录
        moved = _disable_module(module_name, info)

        # 3) 根据移动结果收敛状态：成功→confirmed，失败→回滚
        if moved:
            pending_record["status"] = "confirmed"
            try:
                _atomic_write_json(_state_path(), state)
            except Exception as e:
                # 移动已发生但后续落盘失败：保留 confirmed 记录，
                # 下次启动时 _reconcile_pending 会按路径存在与否重新对齐。
                log.warning(
                    "auto_node_disable: failed to persist confirmed disable for %s: %s",
                    module_name, e,
                )
            newly.append(module_name)
        else:
            disabled.pop(module_name, None)
            try:
                _atomic_write_json(_state_path(), state)
            except Exception as e:
                log.warning(
                    "auto_node_disable: failed to persist rollback for %s: %s",
                    module_name, e,
                )
            log.warning(
                "auto_node_disable: rolled back disabled record for %s after move failure",
                module_name,
            )

    if newly and not dry_run:
        log.info(
            "auto_node_disable: auto-disabled %s after %s rounds of disuse: %s",
            len(newly), threshold, newly,
        )

    return newly


def _disable_module(module_name: str, info: dict[str, Any]) -> bool:
    """把模块（目录或单文件）移动到 ``custom_nodes/.disabled/<原名>/``。"""
    src = info.get("module_path", "")
    if not src or not os.path.exists(src):
        log.warning(
            "auto_node_disable: skip %s, path not found: %s", module_name, src
        )
        return False

    dst_dir = _disabled_dir()
    try:
        os.makedirs(dst_dir, exist_ok=True)
    except Exception as e:
        log.warning("auto_node_disable: cannot create %s: %s", dst_dir, e)
        return False

    # 避免命名冲突：如果 .disabled 下已有同名，加时间戳后缀
    base_target = os.path.join(dst_dir, module_name)
    target = base_target
    if os.path.exists(target):
        target = base_target + ".__" + str(int(time.time()))
    try:
        shutil.move(src, target)
        log.info("auto_node_disable: moved %s -> %s", src, target)
        return True
    except Exception as e:
        log.warning("auto_node_disable: failed to move %s: %s", src, e)
        return False


# ---------------------------------------------------------------------------
# 手动恢复 / 查询 / 配置变更
# ---------------------------------------------------------------------------

def restore_module(module_name: str) -> bool:
    """把 ``.disabled/<module_name>`` 移回 ``custom_nodes/<module_name>``。

    外层负责获取状态锁；真正的物理移动与状态清理交给
    ``_restore_disabled_module_unsafe``，便于下面的自动恢复路径复用同一份代码。
    """
    with _state_lock:
        state = _load_state()
        return _restore_disabled_module_unsafe(state, module_name)


def _restore_disabled_module_unsafe(state: dict[str, Any], module_name: str) -> bool:
    """``restore_module`` 的核心实现。**调用方必须已持有 ``_state_lock``**。

    把 ``.disabled/<module_name>`` 移回 ``custom_nodes/<module_name>``，
    并从 ``state["disabled"]`` 中清理该条目。返回是否成功。

    出现任意错误（例如目标位置已被占用、移动失败等）都会返回 False，
    并保留 ``state["disabled"]`` 原样，以便用户手动恢复或重试。
    """
    disabled = state.get("disabled", {}) or {}
    info = disabled.get(module_name)
    if not info:
        # 尝试直接定位
        candidate = os.path.join(_disabled_dir(), module_name)
        if not os.path.exists(candidate):
            log.info("auto_node_disable: %s is not currently disabled", module_name)
            return False
        target_src = candidate
    else:
        target_src = info.get("original_path") or os.path.join(
            _disabled_dir(), module_name
        )
        if not os.path.exists(target_src):
            target_src = os.path.join(_disabled_dir(), module_name)
        if not os.path.exists(target_src):
            log.info(
                "auto_node_disable: cannot find disabled module at %s", target_src
            )
            return False

    dst = os.path.join(_custom_nodes_dir(), module_name)
    if os.path.exists(dst):
        log.warning(
            "auto_node_disable: destination %s already exists; aborting restore",
            dst,
        )
        return False

    try:
        shutil.move(target_src, dst)
    except Exception as e:
        log.warning("auto_node_disable: failed to restore %s: %s", target_src, e)
        return False

    disabled.pop(module_name, None)
    _save_state(state)
    log.info("auto_node_disable: restored %s -> %s", target_src, dst)
    return True


# ---------------------------------------------------------------------------
# 缺失节点类自动恢复：扫描 .disabled/ 里能提供这些类名的模块并恢复
# ---------------------------------------------------------------------------

def _current_registered_classes() -> Optional[set[str]]:
    """返回当前 ComfyUI 进程里实际注册过的节点类名集合。

    - 返回 ``None`` 表示“未知”（=无法导入 ``nodes`` 模块或映射不可用），
      调用方应保守地不做任何恢复。
    - 返回 ``set``（可能为空）表示已知集合；若为空说明进程里没有任何节点
      类被注册，本次用到的任何 class 都属于“缺失”。
    仅取 key 名称，不实际触发任何节点加载。
    """
    try:
        import nodes  # ComfyUI 全局
        mapping = getattr(nodes, "NODE_CLASS_MAPPINGS", None)
        if not isinstance(mapping, dict):
            return None
        return {str(k) for k in mapping.keys() if k}
    except Exception:
        return None


def restore_for_missing_classes(
    state: dict[str, Any],
    used_classes: Iterable[str],
    prompt_id: Optional[str] = None,
) -> list[str]:
    """本次入队用了某些节点类，如果它们当前**未被注册**，就去 ``.disabled/`` 里
    找匹配模块恢复；返回恢复的模块名列表（用于追加到 ``pending_restart``）。

    设计要点
    --------

    1. 通过 ``_current_registered_classes`` 拿到当前进程真实注册过的类集合；
       用 ``used_classes - registered`` 得到“缺失”类。
    2. 遍历 ``state["disabled"]`` 里所有 ``status="confirmed"`` 的模块，按其
       ``node_classes`` 与缺失类做交集；只要命中就触发原子恢复（复用
       ``_restore_disabled_module_unsafe`` 的三步流程）。
    3. 一个缺失类只能匹配到一个模块（第一个命中即消费该类，避免同一模块被
       多轮恢复；其他类继续向下找）。
    4. 不修改 ``rounds`` 与 ``used_union``：恢复操作不应该反过来触发自动禁用。
    5. 若 ``nodes`` 模块导入失败（=拿不到 registered 集合），直接返回空，
       不做任何恢复，避免误操作。

    **调用方必须已持有 ``_state_lock``**。
    """
    used_set = {c for c in (used_classes or []) if isinstance(c, str) and c}
    if not used_set:
        return []

    registered = _current_registered_classes()
    if registered is None:
        # “未知”状态：无法判断哪些类是真“缺失”，保守地不做任何恢复
        return []

    missing = used_set - registered
    if not missing:
        return []

    disabled: dict[str, Any] = state.get("disabled") or {}
    if not disabled:
        return []

    restored: list[str] = []
    pending_restart: list[dict[str, Any]] = list(state.get("pending_restart") or [])

    # 复制 keys 后再迭代，避免恢复过程中字典被修改
    for module_name, info in list(disabled.items()):
        if not isinstance(info, dict):
            continue
        # 只考虑已确认被禁用的（confirmed）；pending / dry_run 不在文件系统上，
        # 无需也无法物理恢复
        if info.get("status") != "confirmed":
            continue
        node_classes = set(info.get("node_classes") or [])
        if not node_classes:
            # 历史记录里没有 node_classes（旧版本写入的），跳过以免误伤
            continue
        hit = node_classes & missing
        if not hit:
            continue

        # 命中：尝试原子恢复。失败也不抛出，让其它候选继续。
        ok = _restore_disabled_module_unsafe(state, module_name)
        if not ok:
            log.warning(
                "auto_node_disable: failed to auto-restore %s for missing classes %s",
                module_name, sorted(hit),
            )
            continue

        restored.append(module_name)
        # 从 missing 中扣除已恢复覆盖的类，避免被其它模块重复消费
        missing -= node_classes
        pending_restart.append({
            "module": module_name,
            "node_classes": sorted(node_classes),
            "restored_at": time.time(),
            "prompt_id": prompt_id,
        })

        if not missing:
            break

    if pending_restart:
        state["pending_restart"] = pending_restart
        # 写盘：让前端即便错过实时通知也能在下一次 /status 看到
        try:
            _atomic_write_json(_state_path(), state)
        except Exception as e:
            log.warning(
                "auto_node_disable: failed to persist pending_restart: %s", e,
            )

    if restored:
        log.info(
            "auto_node_disable: auto-restored %d disabled module(s) for missing classes: %s",
            len(restored), restored,
        )

    return restored


def consume_pending_restart() -> list[dict[str, Any]]:
    """前端在拿到重启提示后调用本接口消费一次 ``pending_restart``。

    返回当前待提示的条目列表（消费后会被清空）。
    """
    with _state_lock:
        state = _load_state()
        items = list(state.get("pending_restart") or [])
        if items:
            state["pending_restart"] = []
            _save_state(state)
        return items


def set_threshold(value: int) -> None:
    """运行时调整阈值并立即持久化。"""
    with _state_lock:
        state = _load_state()
        state["threshold"] = max(0, int(value))
        _save_state(state)


def set_exclude(names: Iterable[str]) -> None:
    """运行时调整排除列表。"""
    with _state_lock:
        state = _load_state()
        state["exclude"] = sorted(set(names))
        _save_state(state)


def set_dry_run(enabled: bool) -> None:
    """运行时开关干跑模式。

    开启后，``_decide`` 只会把候选模块写入 ``disabled`` 字典（带
    ``status="dry_run"``），不会实际移动目录。关闭后下一次入队触发决策
    时就会按正常流程移动；之前标记为 ``dry_run`` 的条目不会被自动迁移，
    需要通过 ``restore_module`` 或手动清理。
    """
    with _state_lock:
        state = _load_state()
        state["dry_run"] = bool(enabled)
        _save_state(state)


def snapshot() -> dict[str, Any]:
    """获取当前状态（只读快照）。"""
    with _state_lock:
        state = _load_state()
        # 深拷贝一次避免外部修改
        return json.loads(json.dumps(state))


# ---------------------------------------------------------------------------
# 暴露给入口模块的提取工具
# ---------------------------------------------------------------------------

def extract_used_class_names(prompt_or_json: Any) -> list[str]:
    """从 ``/prompt`` API 的请求体中提取本次工作流实际使用的节点类集合。

    ComfyUI 的 ``/prompt`` 接口请求体形如：
        {
            "prompt": {<node_id>: {"class_type": "...", "inputs": {...}}, ...},
            "client_id": "...",
            "extra_data": {...},
            ...
        }
    ``onprompt`` 钩子拿到的就是这个完整 dict；这里做兼容：

    - 如果传入的是纯 prompt（顶层就是 ``{<id>: {class_type, ...}}``），直接解析；
    - 否则尝试从 ``payload["prompt"]`` 取。
    """
    used: set[str] = set()

    def _collect(d: Any) -> None:
        if not isinstance(d, dict):
            return
        for _node_id, node_def in d.items():
            if isinstance(node_def, dict):
                ct = node_def.get("class_type")
                if isinstance(ct, str) and ct:
                    used.add(ct)

    if not isinstance(prompt_or_json, dict):
        return []
    # 1) 纯 prompt：每个 value 都含 class_type（绝大多数 value 都应满足）
    def _value_has_class_type(v: Any) -> bool:
        return isinstance(v, dict) and isinstance(v.get("class_type"), str)
    looks_like_prompt = any(_value_has_class_type(v) for v in prompt_or_json.values())
    if looks_like_prompt:
        _collect(prompt_or_json)
        return sorted(used)
    # 2) 包装形态：从 "prompt" 字段再取
    inner = prompt_or_json.get("prompt")
    if isinstance(inner, dict):
        _collect(inner)
    return sorted(used)
