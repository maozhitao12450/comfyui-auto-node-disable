"""记录一次『页面打开』事件（即一次 prompt 入队）的入口。

本子模块是插件热路径的主入口：每次 ComfyUI ``onprompt`` 钩子触发时，
调用方通过 ``auto_disable.record_prompt(payload, prompt_id=...)`` 进入，
本函数负责

1. 从 payload 抽取本次用到的节点类名；
2. 触发缺失节点类的自动恢复（``restore_for_missing_classes``）；
3. 写入一条 ``rounds`` 记录并修剪窗口大小；
4. 调用 ``_decide`` 做一次决策。

调用方约定
----------
所有依赖（路径、锁、状态 I/O、扫描器、决策器、恢复器等）必须通过
``auto_disable._xxx()`` 走包级属性查找，以确保 ``mock.patch.object(
auto_disable, ...)`` 在测试中可见。
"""

from __future__ import annotations

import time
from typing import Any, Optional

import auto_disable


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
        used_class_names = auto_disable.extract_used_class_names(prompt_or_json)
        if prompt_id is None:
            prompt_id = prompt_or_json.get("prompt_id")
    else:
        used_class_names = prompt_or_json or []

    # 规范化入队关联标识：仅保留非空字符串
    if prompt_id is not None:
        prompt_id = str(prompt_id) if str(prompt_id) else None

    with auto_disable._state_lock:
        state = auto_disable._load_state()
        # ``known_modules`` 由 ``_load_state`` 在进程启动时一次性扫描并持久化；
        # 运行中新增模块通过 ``restore_for_missing_classes`` 自动回填，
        # 重装 / 卸载场景请调用 ``refresh_known_modules`` 手动刷新。
        # 此处不再每次 record_prompt 都扫，避免每条 prompt 都遍历全局映射。

        used = sorted(set(used_class_names))

        # 1) 缺失节点类自动恢复：在写入本轮 round 之前尝试把已禁用的
        #    能提供本次所需类名的模块移回 custom_nodes/。
        #    若有命中，会把待重启条目写入 state["pending_restart"]，
        #    前端在 prompt 响应后再发起一次 /auto_disable/status 拉取即可拿到。
        try:
            auto_disable.restore_for_missing_classes(
                state, used, prompt_id=prompt_id
            )
        except Exception as e:
            auto_disable.log.warning(
                "auto_node_disable: auto-restore step failed: %s", e
            )

        round_entry: dict[str, Any] = {"timestamp": time.time(), "used_classes": used}
        if prompt_id is not None:
            round_entry["prompt_id"] = prompt_id
        state["rounds"].append(round_entry)

        # 修剪滚动窗口大小：保留足够做决策的窗口
        keep = max(int(state.get("threshold", auto_disable.DEFAULT_THRESHOLD)) + 2, 5)
        if len(state["rounds"]) > keep * 4:
            state["rounds"] = state["rounds"][-keep * 4:]

        auto_disable._save_state(state)

        # 即时决策（传入本次入队标识以便写入 disabled 审计字段）
        newly_disabled: list[str] = []
        try:
            newly_disabled = auto_disable._decide(
                state, last_prompt_id=prompt_id
            ) or []
        except Exception as e:
            auto_disable.log.warning(
                "auto_node_disable: decision step failed: %s", e
            )
        else:
            auto_disable._save_state(state)

        # 返回本次入队后的进度摘要，供调用方记录到日志/遥测。
        disabled_map = state.get("disabled") or {}
        return {
            "rounds_count": len(state["rounds"]),
            "threshold": int(state.get("threshold", auto_disable.DEFAULT_THRESHOLD)),
            "keep": keep,
            "cap": keep * 4,
            "dry_run": bool(state.get("dry_run", False)),
            "known_count": len(state.get("known_modules") or {}),
            "newly_disabled": list(newly_disabled),
            "disabled_count": len(disabled_map),
        }