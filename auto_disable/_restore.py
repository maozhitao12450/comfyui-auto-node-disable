"""恢复：手动恢复 / 缺失节点类自动恢复。

两条恢复路径共用一个底层搬运函数 ``_restore_disabled_module_unsafe``：

- ``restore_module``：用户从前端或 API 主动发起恢复；
- ``restore_for_missing_classes``：在 ``record_prompt`` 写入新 round 前
  自动扫描 ``.disabled/`` 里能补齐缺失类名的模块并恢复。

调用方约定
----------
所有依赖（路径常量、状态持久化、状态文件路径、_state_lock 等）必须通过
``auto_disable._xxx()`` 走包级属性查找。
"""

from __future__ import annotations

import os
import shutil
import time
from typing import Any, Iterable, Optional

import auto_disable


def restore_module(module_name: str) -> bool:
    """把 ``.disabled/<module_name>`` 移回 ``custom_nodes/<module_name>``。

    外层负责获取状态锁；真正的物理移动与状态清理交给
    ``restore_disabled_module_unsafe``，便于下面的自动恢复路径复用同一份代码。
    """
    with auto_disable._state_lock:
        state = auto_disable._load_state()
        return auto_disable._restore_disabled_module_unsafe(state, module_name)


def restore_disabled_module_unsafe(
    state: dict[str, Any], module_name: str
) -> bool:
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
        candidate = os.path.join(auto_disable._disabled_dir(), module_name)
        if not os.path.exists(candidate):
            auto_disable.log.info(
                "auto_node_disable: %s is not currently disabled", module_name
            )
            return False
        target_src = candidate
    else:
        target_src = info.get("original_path") or os.path.join(
            auto_disable._disabled_dir(), module_name
        )
        if not os.path.exists(target_src):
            target_src = os.path.join(auto_disable._disabled_dir(), module_name)
        if not os.path.exists(target_src):
            auto_disable.log.info(
                "auto_node_disable: cannot find disabled module at %s", target_src
            )
            return False

    dst = os.path.join(auto_disable._custom_nodes_dir(), module_name)
    if os.path.exists(dst):
        auto_disable.log.warning(
            "auto_node_disable: destination %s already exists; aborting restore",
            dst,
        )
        return False

    try:
        shutil.move(target_src, dst)
    except Exception as e:
        auto_disable.log.warning(
            "auto_node_disable: failed to restore %s: %s", target_src, e
        )
        return False

    disabled.pop(module_name, None)
    auto_disable._save_state(state)
    auto_disable.log.info(
        "auto_node_disable: restored %s -> %s", target_src, dst
    )
    return True


def current_registered_classes() -> Optional[set[str]]:
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

    1. 通过 ``current_registered_classes`` 拿到当前进程真实注册过的类集合；
       用 ``used_classes - registered`` 得到“缺失”类。
    2. 遍历 ``state["disabled"]`` 里所有 ``status="confirmed"`` 的模块，按其
       ``node_classes`` 与缺失类做交集；只要命中就触发原子恢复（复用
       ``restore_disabled_module_unsafe`` 的三步流程）。
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

    registered = auto_disable._current_registered_classes()
    if registered is None:
        # “未知”状态：无法判断哪些类是真“缺失”，保守地不做任何恢复
        return []

    missing = used_set - registered
    if not missing:
        return []

    disabled: dict[str, Any] = state.get("disabled") or {}
    if not disabled:
        # 有 missing 类但 disabled 字典是空的（不应该发生除非 state 被重置）。
        auto_disable.log.info(
            "auto_node_disable: missing classes %s but state['disabled'] is empty; "
            "nothing to auto-restore",
            sorted(missing),
        )
        return []

    restored: list[str] = []
    pending_restart: list[dict[str, Any]] = list(state.get("pending_restart") or [])
    candidates_with_classes = 0
    candidates_skipped_no_classes = 0

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
            candidates_skipped_no_classes += 1
            continue
        candidates_with_classes += 1
        hit = node_classes & missing
        if not hit:
            continue

        # 命中：尝试原子恢复。失败也不抛出，让其它候选继续。
        ok = auto_disable._restore_disabled_module_unsafe(state, module_name)
        if not ok:
            auto_disable.log.warning(
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

        # 把刚恢复的模块加入 ``known_modules``，确保下一轮 ``_decide`` 把它
        # 视为“已知且当前可用”，不会因为“该模块不在 known_modules”被绕过禁用，
        # 也避免下一轮 ``record_prompt`` 又需要依赖启动扫描的回填。
        # ``_load_state`` 只在进程启动时一次性扫描，所以恢复时必须主动回填。
        known = state.setdefault("known_modules", {})
        if module_name not in known:
            known[module_name] = {
                "node_classes": sorted(node_classes),
                "module_path": info.get("original_path", ""),
            }

        if not missing:
            break

    if pending_restart:
        state["pending_restart"] = pending_restart
        # 写盘：让前端即便错过实时通知也能在下一次 /status 看到
        try:
            auto_disable._atomic_write_json(auto_disable._state_path(), state)
        except Exception as e:
            auto_disable.log.warning(
                "auto_node_disable: failed to persist pending_restart: %s", e,
            )

    if restored:
        auto_disable.log.info(
            "auto_node_disable: auto-restored %d disabled module(s) for missing classes: %s",
            len(restored), restored,
        )
    elif candidates_with_classes == 0 and candidates_skipped_no_classes > 0:
        # 有 missing 类、但所有 disabled 条目都没有 node_classes（陈旧记录）。
        # 这种情况 reconcile 启动对账能修复：启动时扫描这些模块拿 NODE_CLASS_MAPPINGS。
        auto_disable.log.warning(
            "auto_node_disable: missing classes %s not auto-restored: "
            "all %d disabled entries lack node_classes (legacy records). "
            "Restart ComfyUI so reconcile can scan them.",
            sorted(set(used_set) - registered),
            candidates_skipped_no_classes,
        )
    elif candidates_with_classes > 0 and missing:
        # 有 node_classes 的候选但都没命中——告诉用户哪些类还在漂
        auto_disable.log.info(
            "auto_node_disable: missing classes %s not matched by any of the "
            "%d disabled modules with node_classes; "
            "the providing module may have been deleted or renamed",
            sorted(missing), candidates_with_classes,
        )

    return restored