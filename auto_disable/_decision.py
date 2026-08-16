"""决策：根据滚动窗口判定哪些 custom_node 应该被禁用。

每次 ``record_prompt`` 在写入新 round 后会调用 ``_decide``，根据最近
``threshold`` 轮的使用情况，找出"所有节点类都未被使用"的模块并执行三步
安全边界（pending 落盘 → 物理移动 → confirmed 落盘 / 回滚）。

调用方约定
----------
所有依赖（路径常量、原子写入、状态文件路径、_disabled_dir 等）必须通过
``auto_disable._xxx()`` 走包级属性查找。
"""

from __future__ import annotations

import os
import shutil
import time
from typing import Any, Optional

import auto_disable


def decide(
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
    threshold = int(state.get("threshold", auto_disable.DEFAULT_THRESHOLD))
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

    exclude = set(state.get("exclude", []) or auto_disable.DEFAULT_EXCLUDE)
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
            auto_disable.log.info(
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
            auto_disable._atomic_write_json(auto_disable._state_path(), state)
        except Exception as e:
            # 持久化失败：不进入移动步骤，以免出现“未记录的移动”
            disabled.pop(module_name, None)
            auto_disable.log.warning(
                "auto_node_disable: failed to persist pending disable for %s: %s",
                module_name, e,
            )
            continue

        # 2) 实际移动目录
        moved = disable_module(module_name, info)

        # 3) 根据移动结果收敛状态：成功→confirmed，失败→回滚
        if moved:
            pending_record["status"] = "confirmed"
            try:
                auto_disable._atomic_write_json(auto_disable._state_path(), state)
            except Exception as e:
                # 移动已发生但后续落盘失败：保留 confirmed 记录，
                # 下次启动时 _reconcile_pending 会按路径存在与否重新对齐。
                auto_disable.log.warning(
                    "auto_node_disable: failed to persist confirmed disable for %s: %s",
                    module_name, e,
                )
            newly.append(module_name)
        else:
            disabled.pop(module_name, None)
            try:
                auto_disable._atomic_write_json(auto_disable._state_path(), state)
            except Exception as e:
                auto_disable.log.warning(
                    "auto_node_disable: failed to persist rollback for %s: %s",
                    module_name, e,
                )
            auto_disable.log.warning(
                "auto_node_disable: rolled back disabled record for %s after move failure",
                module_name,
            )

    if newly and not dry_run:
        auto_disable.log.info(
            "auto_node_disable: auto-disabled %s after %s rounds of disuse: %s",
            len(newly), threshold, newly,
        )

    return newly


def disable_module(module_name: str, info: dict[str, Any]) -> bool:
    """把模块（目录或单文件）移动到 ``custom_nodes/.disabled/<原名>/``。"""
    src = info.get("module_path", "")
    if not src or not os.path.exists(src):
        auto_disable.log.warning(
            "auto_node_disable: skip %s, path not found: %s", module_name, src
        )
        return False

    dst_dir = auto_disable._disabled_dir()
    try:
        os.makedirs(dst_dir, exist_ok=True)
    except Exception as e:
        auto_disable.log.warning(
            "auto_node_disable: cannot create %s: %s", dst_dir, e
        )
        return False

    # 避免命名冲突：如果 .disabled 下已有同名，加时间戳后缀
    base_target = os.path.join(dst_dir, module_name)
    target = base_target
    if os.path.exists(target):
        target = base_target + ".__" + str(int(time.time()))
    try:
        shutil.move(src, target)
        auto_disable.log.info("auto_node_disable: moved %s -> %s", src, target)
        return True
    except Exception as e:
        auto_disable.log.warning(
            "auto_node_disable: failed to move %s: %s", src, e
        )
        return False