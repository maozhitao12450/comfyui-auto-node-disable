"""
auto_disable
============
核心模块：追踪 ComfyUI custom_nodes 中各节点模块的使用情况，
并在连续若干次『打开页面（即入队 prompt）』未被使用时，
将其整体移动到 ``ComfyUI/custom_nodes/.disabled/<原名>/`` 子目录中禁用。

设计要点
--------
- 通过遍历全局 ``NODE_CLASS_MAPPINGS``，利用每个节点类上的
  ``RELATIVE_PYTHON_MODULE`` 属性，反向构建
  ``custom_node 子目录名 -> 它提供的节点类名集合`` 的映射。
- 每次有工作流入队（``prompt`` API），都从 prompt 中提取本次用到的节点类
  并写入一份滚动状态（默认放在插件目录内 ``auto_node_disable_state.db``，
  SQLite 数据库；与本文件同目录）。历史版本使用同名 ``.json`` 文件，
  首次启动会自动迁移并归档为 ``.json.migrated``。
- 当滚动窗口内连续 ``threshold``（默认 30）次入队都未出现某 custom_node 的节点类时，
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

模块布局
--------
本目录拆为多个子模块以控制单文件长度；所有子模块都通过包级属性查找
（``auto_disable._xxx()``）访问基础能力，这样测试里的
``mock.patch.object(auto_disable, "_state_path", ...)`` 在子模块函数体内依然可见。
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
import time
from typing import Any, Iterable, Optional

# Python 模块对象的 __dict__ 不会自动包含模块自身名，但子模块函数体
# 里需要用 ``auto_disable._xxx()`` 这种包级查找访问基础能力（保持 mock
# 可见）。在 __init__ 顶部显式 ``import auto_disable`` 把名字绑定到本模块
# globals，让 ``_atomic_write_json`` / ``_load_state`` 等函数体内能写
# ``auto_disable._storage.xxx()`` 而不报 NameError。
import auto_disable  # noqa: F401  (used for global name binding only)

log = logging.getLogger("auto_node_disable")


# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------

# 状态文件名（SQLite 数据库，保留在插件目录内与代码同目录，便于分发迁移）。
# 历史版本同名 ``.json`` 文件会在首次启动时被 ``_storage.migrate_json_to_db``
# 自动读取并归档为 ``.json.migrated``。
STATE_FILENAME = "auto_node_disable_state.db"

# 旧版 JSON 文件名（历史兼容常量）。与 ``STATE_FILENAME`` 解耦：旧版使用
# ``.json`` 后缀，新版使用 ``.db``。迁移过程中需要独立识别旧位置的文件名。
LEGACY_STATE_FILENAME = "auto_node_disable_state.json"

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


def _plugin_dir() -> str:
    """本插件所在目录（即 ``auto_disable.py`` 所在目录）。"""
    return os.path.dirname(os.path.abspath(__file__))


def _state_path() -> str:
    """当前状态文件路径：与插件代码同目录。

    之前版本写在 ``ComfyUI/auto_node_disable_state.json``（ComfyUI 根目录），
    现改为插件目录内（旧位置会在启动时自动迁移）。
    """
    return os.path.join(_plugin_dir(), STATE_FILENAME)


def _legacy_state_path() -> str:
    """旧版 JSON 状态文件位置：ComfyUI 根目录（``custom_nodes`` 的父目录）。

    历史上状态文件一直叫 ``auto_node_disable_state.json``（迁移到 SQLite 后
    才改名 ``.db``），所以这里直接写死 ``.json``，避免和现行 ``STATE_FILENAME``
    不同后缀时反而读不到旧文件。
    """
    return os.path.join(_comfy_root(), LEGACY_STATE_FILENAME)


def _migrate_legacy_state() -> None:
    """把旧位置的 ``auto_node_disable_state.json`` 迁移到 SQLite 数据库。

    实际迁移逻辑委托给 ``auto_disable._storage.migrate_json_to_db``：
    - 旧 JSON 不存在：no-op
    - 新 DB 已存在：以新为准，旧 JSON 文件被归档为 ``.migrated`` 并记 info
    - 同路径（测试 / 插件恰好就装在 ComfyUI 根目录）：no-op
    - 否则读取 JSON → 写入 SQLite → ``os.replace`` 归档原文件
    """
    legacy = _legacy_state_path()
    new = _state_path()
    try:
        if os.path.abspath(legacy) == os.path.abspath(new):
            return
    except Exception:
        return
    # 无论迁移是否真发生，``migrate_json_to_db`` 都会处理归档；
    # 失败时它内部会记 warning，不会抛出。
    auto_disable._storage.migrate_json_to_db(legacy, new)


def _disabled_dir() -> str:
    return os.path.join(_custom_nodes_dir(), DISABLED_DIR_NAME)


# ---------------------------------------------------------------------------
# 状态管理（线程安全 + 原子写）
# ---------------------------------------------------------------------------

_state_lock = threading.RLock()


def _load_state() -> dict[str, Any]:
    """读取状态；不存在则返回默认结构。

    读路径：
    1. 启动时执行 ``_migrate_legacy_state``，把旧版 ``.json`` 导入 SQLite 并归档。
    2. 走 ``auto_disable._storage.load_state_from_db`` 从 SQLite 装载 dict。
       任何读错误（文件缺失、表损坏、JSON 反序列化失败）都会被 ``load_state_from_db``
       吞掉并落回默认状态，绝不抛出。
    3. 启动时对齐 ``pending`` 与 ``disabled``，如果对账造成变更则回写 SQLite。
    """
    # 启动时把旧位置（ComfyUI 根目录）的 JSON 状态迁移到 SQLite 数据库
    _migrate_legacy_state()
    data = auto_disable._storage.load_state_from_db(_state_path())
    # 兼容旧结构：补齐缺失字段（防御性，正常路径 load_state_from_db 已补齐）
    defaults = _default_state()
    for k, v in defaults.items():
        data.setdefault(k, v)
    if not isinstance(data.get("pending_restart"), list):
        data["pending_restart"] = []
    # 启动时对齐 pending 与目录实际位置（进程崩溃后恢复用）
    pending_changed = _reconcile_pending(data)
    # 启动时对齐 disabled 与 .disabled/ 磁盘：补齐手工禁用、清理被手动恢复的条目
    disk_result = _reconcile_disabled_with_disk(data)
    if pending_changed or disk_result["changed"]:
        try:
            _atomic_write_json(_state_path(), data)
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
        # 注意：``dry_run`` 条目从未实际移动过目录，原路径必然存在，
        # 但这是“预计会被移动”而非“已被用户手动恢复”，因此跳过此分支。
        original = (info.get("original_path") or "").strip()
        if info.get("status") != "dry_run" and original and os.path.exists(original):
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
        # 主动 import 该模块提取 NODE_CLASS_MAPPINGS，以便后续
        # restore_for_missing_classes 能按类名匹配。
        disabled_path = os.path.join(ddir, name)
        node_classes = _extract_node_classes_from_path(disabled_path)
        # node_classes=None 表示扫描失败（异常/路径不存在），区别于
        # 加载成功但未声明映射（[]）。
        if node_classes is None:
            log.warning(
                "auto_node_disable: reconcile: added %s to state['disabled'] "
                "but could not extract NODE_CLASS_MAPPINGS; "
                "auto-restore by class name will not work for this module",
                name,
            )
            node_classes = []
        else:
            log.info(
                "auto_node_disable: reconcile: added %s to state['disabled'] "
                "(scanned %d node_classes: %s)",
                name, len(node_classes), node_classes,
            )
        disabled[name] = {
            "original_path": "",
            "disabled_at": time.time(),
            "status": "confirmed",
            "node_classes": node_classes,
        }
        changed = True
        added.append(name)

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
    """原子化持久化 ``state``。

    历史上该函数直接写 JSON 文件；2026-08-17 改造为 SQLite 存储后保留
    函数名与签名，内部委托给 ``auto_disable._storage.save_state_to_db``，
    这样 ``verify_hot_path.py`` 的故障注入与既有测试中的 mock patch
    不需要改动。
    """
    auto_disable._storage.save_state_to_db(path, data)


def _save_state(state: dict[str, Any]) -> None:
    """写入状态；任何异常都吞掉并告警，避免污染调用方热路径。"""
    try:
        _atomic_write_json(_state_path(), state)
    except Exception as e:
        log.warning("auto_node_disable: failed to persist state: %s", e)


# ---------------------------------------------------------------------------
# 子模块导入与符号再导出
#
# 顺序很关键：上面定义的所有 ``_xxx`` 基础能力必须先存在，再 import 子模块，
# 子模块函数体内通过 ``auto_disable._xxx()`` 这种包级属性查找调用它们。
# 这样测试里 ``mock.patch.object(auto_disable, "_state_path", ...)`` 才能
# 在子模块函数体内被看到。
#
# 再导出（re-bind）发生在子模块导入之后，目的是让外部继续以
# ``auto_disable._decide`` / ``auto_disable.record_prompt`` 这种扁平方式调用，
# 与拆分前完全兼容。
# ---------------------------------------------------------------------------

from auto_disable import _scanner as _scanner  # noqa: E402
from auto_disable import _record as _record  # noqa: E402
from auto_disable import _decision as _decision  # noqa: E402
from auto_disable import _restore as _restore  # noqa: E402
from auto_disable import _api as _api  # noqa: E402
from auto_disable import _storage as _storage  # noqa: E402

# SQLite 存储层（_storage.py）由 _load_state / _save_state / _migrate_legacy_state
# 通过包级属性查找调用，对外不直接暴露但保留在 ``__all__`` 中以便测试与诊断脚本使用。

# 反向映射（_scanner.py）
_scan_known_modules = _scanner.scan_known_modules
_resolve_module_path = _scanner.resolve_module_path
_extract_node_classes_from_path = _scanner.extract_node_classes_from_path

# 决策（_decision.py）
_decide = _decision.decide
_disable_module = _decision.disable_module

# 恢复（_restore.py）
_restore_disabled_module_unsafe = _restore.restore_disabled_module_unsafe
_current_registered_classes = _restore.current_registered_classes

# 入口与对外 API（_record.py / _restore.py / _api.py）
record_prompt = _record.record_prompt
restore_module = _restore.restore_module
restore_for_missing_classes = _restore.restore_for_missing_classes
consume_pending_restart = _api.consume_pending_restart
set_threshold = _api.set_threshold
set_exclude = _api.set_exclude
set_dry_run = _api.set_dry_run
snapshot = _api.snapshot
extract_used_class_names = _api.extract_used_class_names


__all__ = [
    # 常量
    "STATE_FILENAME",
    "LEGACY_STATE_FILENAME",
    "DISABLED_DIR_NAME",
    "DEFAULT_THRESHOLD",
    "DEFAULT_EXCLUDE",
    # 路径工具
    "_custom_nodes_dir",
    "_comfy_root",
    "_plugin_dir",
    "_state_path",
    "_legacy_state_path",
    "_migrate_legacy_state",
    "_disabled_dir",
    # 状态管理
    "_state_lock",
    "_load_state",
    "_reconcile_disabled_with_disk",
    "_reconcile_pending",
    "_default_state",
    "_atomic_write_json",
    "_save_state",
    # SQLite 存储层
    "_storage",
    # 反向映射
    "_scan_known_modules",
    "_resolve_module_path",
    "_extract_node_classes_from_path",
    # 决策
    "_decide",
    "_disable_module",
    # 恢复
    "_restore_disabled_module_unsafe",
    "_current_registered_classes",
    # 对外入口
    "record_prompt",
    "restore_module",
    "restore_for_missing_classes",
    "consume_pending_restart",
    "set_threshold",
    "set_exclude",
    "set_dry_run",
    "snapshot",
    "extract_used_class_names",
    "log",
]


# Silence unused-import warnings for symbols that are only re-exported.
_ = (shutil, sys, Iterable, Optional)