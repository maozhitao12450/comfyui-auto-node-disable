"""对外 API：配置变更与查询接口。

本子模块集中放所有运行时配置入口（阈值、排除列表、干跑模式、状态快照、
重启提示消费），以及 ``extract_used_class_names`` 这个被 prompt 解析路径
依赖的纯函数工具。

调用方约定
----------
所有依赖（路径常量、原子写入、状态文件路径、_state_lock 等）必须通过
``auto_disable._xxx()`` 走包级属性查找。
"""

from __future__ import annotations

import json
from typing import Any, Iterable

import auto_disable


def consume_pending_restart() -> list[dict[str, Any]]:
    """前端在拿到重启提示后调用本接口消费一次 ``pending_restart``。

    返回当前待提示的条目列表（消费后会被清空）。
    """
    with auto_disable._state_lock:
        state = auto_disable._load_state()
        items = list(state.get("pending_restart") or [])
        if items:
            state["pending_restart"] = []
            auto_disable._save_state(state)
        return items


def set_threshold(value: int) -> None:
    """运行时调整阈值并立即持久化。"""
    with auto_disable._state_lock:
        state = auto_disable._load_state()
        state["threshold"] = max(0, int(value))
        auto_disable._save_state(state)


def set_exclude(names: Iterable[str]) -> None:
    """运行时调整排除列表。"""
    with auto_disable._state_lock:
        state = auto_disable._load_state()
        state["exclude"] = sorted(set(names))
        auto_disable._save_state(state)


def set_dry_run(enabled: bool) -> None:
    """运行时开关干跑模式。

    开启后，``_decide`` 只会把候选模块写入 ``disabled`` 字典（带
    ``status="dry_run"``），不会实际移动目录。关闭后下一次入队触发决策
    时就会按正常流程移动；之前标记为 ``dry_run`` 的条目不会被自动迁移，
    需要通过 ``restore_module`` 或手动清理。
    """
    with auto_disable._state_lock:
        state = auto_disable._load_state()
        state["dry_run"] = bool(enabled)
        auto_disable._save_state(state)


def snapshot() -> dict[str, Any]:
    """获取当前状态（只读快照）。"""
    with auto_disable._state_lock:
        state = auto_disable._load_state()
        # 深拷贝一次避免外部修改
        return json.loads(json.dumps(state))


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