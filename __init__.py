"""
ComfyUI Auto Node Disable
=========================
自动追踪 ComfyUI 中 custom_nodes 各节点模块的使用情况。

行为约定
--------
1. 每次用户向 ComfyUI 提交一次工作流（``/prompt`` 入队），本插件会在
   ``onprompt`` 钩子里记录这次工作流实际用到的节点类型集合；
2. 维护一份滚动窗口（默认保留最近若干次），每次入队后都做一次决策：
   如果某个 custom_node 模块所提供的节点类型在最近 ``threshold`` 次
   入队中都未出现，就把它整体移动到 ``custom_nodes/.disabled/<原名>/``
   子目录里；
3. 阈值和排除列表可持久化到 ComfyUI 根目录下的
   ``auto_node_disable_state.json`` 文件。

需要重启 ComfyUI 才会真正卸载已被禁用的模块（节点注册发生在启动阶段）。

安装
----
把本目录放到 ``ComfyUI/custom_nodes/comfyui-auto-node-disable/`` 下即可。
无需任何额外依赖。
"""

from __future__ import annotations

import logging
from typing import Any

# 一些 ComfyUI 启动时可能缺失的依赖保护
try:
    from server import PromptServer  # ComfyUI 服务端
except Exception:  # pragma: no cover - 极端情况下允许被导入但不挂载
    PromptServer = None

from . import auto_disable


log = logging.getLogger("auto_node_disable")

WEB_DIRECTORY = "./web/js"

__all__ = ["WEB_DIRECTORY"]


# ---------------------------------------------------------------------------
# 服务端钩子：每次 prompt 入队时记录节点使用
# ---------------------------------------------------------------------------

def _on_prompt(json_data: dict[str, Any], *args, **kwargs):
    """``PromptServer.add_on_prompt_handler`` 钩子。

    ComfyUI ``trigger_on_prompt`` 传入的是完整的 ``/prompt`` 请求体：
    ``{"prompt": {...}, "client_id": "...", "extra_data": {...}, ...}``。
    ``auto_disable.extract_used_class_names`` 已兼容两种形态。
    """
    try:
        used = auto_disable.extract_used_class_names(json_data)
        prompt_id = None
        if isinstance(json_data, dict):
            prompt_id = json_data.get("prompt_id")
        log.info(
            "auto_node_disable: recorded prompt %s with %d distinct nodes",
            prompt_id, len(used),
        )
        # 透传 prompt_id 以便写入状态文件的审计字段，
        # 事后可重建“哪个入队导致哪个目录被禁用”的因果链。
        auto_disable.record_prompt(used, prompt_id=prompt_id)
    except Exception as e:
        log.warning("auto_node_disable: on_prompt handler failed: %s", e)

    # 重要：PromptServer 期望钩子返回原始 json_data（或 None），不要吞掉它
    return json_data


# ---------------------------------------------------------------------------
# API 路由（供前端 UI / curl / ComfyUI-Manager 之类调用）
# ---------------------------------------------------------------------------

def _register_routes(server) -> None:
    """注册一组 ``/auto_disable/*`` 路由供前端/外部调用。"""
    try:
        from aiohttp import web
    except Exception:
        log.warning("auto_node_disable: aiohttp unavailable, skipping routes")
        return

    routes = server.routes if hasattr(server, "routes") else None
    if routes is None:
        log.warning("auto_node_disable: PromptServer has no routes attribute")
        return

    @routes.get("/auto_disable/status")
    async def _status(request):
        import json
        return web.json_response(auto_disable.snapshot())

    @routes.post("/auto_disable/restore")
    async def _restore(request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)
        name = body.get("module")
        if not name:
            return web.json_response(
                {"ok": False, "error": "missing 'module'"}, status=400
            )
        ok = auto_disable.restore_module(name)
        return web.json_response({"ok": ok})

    @routes.post("/auto_disable/threshold")
    async def _threshold(request):
        try:
            body = await request.json()
            value = int(body.get("value", auto_disable.DEFAULT_THRESHOLD))
        except Exception:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)
        auto_disable.set_threshold(value)
        return web.json_response({"ok": True, "threshold": value})

    @routes.post("/auto_disable/exclude")
    async def _exclude(request):
        try:
            body = await request.json()
            names = body.get("names", [])
            if not isinstance(names, list):
                raise ValueError("'names' must be a list")
        except Exception as e:
            return web.json_response(
                {"ok": False, "error": f"invalid payload: {e}"}, status=400
            )
        auto_disable.set_exclude([str(n) for n in names])
        return web.json_response({"ok": True})


# ---------------------------------------------------------------------------
# 启动时挂载钩子与路由
# ---------------------------------------------------------------------------

def _setup() -> None:
    """ComfyUI 启动后由 ``PromptServer`` 实例化时调用一次。"""
    if PromptServer is None:
        log.warning("auto_node_disable: PromptServer unavailable; skipping hooks")
        return

    instance = PromptServer.instance
    try:
        instance.add_on_prompt_handler(_on_prompt)
        log.info("auto_node_disable: on_prompt handler registered")
    except Exception as e:
        log.warning("auto_node_disable: failed to register on_prompt: %s", e)

    try:
        _register_routes(instance)
        log.info("auto_node_disable: API routes registered under /auto_disable/*")
    except Exception as e:
        log.warning("auto_node_disable: failed to register routes: %s", e)

    # 启动时同步一次 known_modules（便于用户在 UI 没启动前看到节点列表）
    try:
        snap = auto_disable.snapshot()
        if not snap.get("known_modules"):
            state = auto_disable._load_state()  # noqa: SLF001 - 内部工具
            auto_disable._scan_known_modules(state)  # noqa: SLF001
            auto_disable._save_state(state)  # noqa: SLF001
            log.info("auto_node_disable: scanned known modules at startup")
    except Exception as e:
        log.warning("auto_node_disable: startup scan failed: %s", e)


# 在模块被 ComfyUI 导入时尝试挂载；放在 try/except 中避免主进程启动失败。
try:
    _setup()
except Exception as _e:  # pragma: no cover
    log.warning("auto_node_disable: setup failed: %s", _e)