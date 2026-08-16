"""反向映射：custom_node 子目录 -> 它提供的节点类集合。

该子模块负责遍历 ComfyUI 全局 ``NODE_CLASS_MAPPINGS``，并解析每个
custom_node 模块在磁盘上的实际位置。

调用方约定
----------
本模块函数访问路径常量、状态文件位置等基础能力时，**必须**通过
``auto_disable._xxx()`` 这种包级属性查找方式调用，而不是 ``from
auto_disable import _xxx``。原因：测试代码通过 ``mock.patch.object(
auto_disable, "_state_path", ...)`` 替换实现时，只有包级属性查找
能找到 mock；若改成局部导入，函数体执行时看到的是 import 时的
真实实现，mock 将失效。
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional

import auto_disable


def scan_known_modules(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """遍历全局 ``NODE_CLASS_MAPPINGS``，构建 ``custom_node 模块名 -> 节点类列表`` 映射。

    节点类 ``RELATIVE_PYTHON_MODULE`` 形如 ``"custom_nodes.<module_name>"``；
    直接取 ``.`` 后的最后一段即可得到 custom_node 子目录名。

    V3 扩展（``comfy_entrypoint`` 返回 ``ComfyExtension``）同样会把节点加入
    ``NODE_CLASS_MAPPINGS``，因此同样适用。
    """
    try:
        from nodes import NODE_CLASS_MAPPINGS  # ComfyUI 全局映射
    except Exception as e:
        auto_disable.log.warning(
            "auto_node_disable: cannot import NODE_CLASS_MAPPINGS: %s", e
        )
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
        module_path = resolve_module_path(module_name)
        result[module_name] = {
            "node_classes": sorted(info["node_classes"]),
            "module_path": module_path,
        }

    state["known_modules"] = result
    return result


def resolve_module_path(module_name: str) -> str:
    """根据模块名定位它在 ``custom_nodes`` 下的真实路径（目录或单文件）。"""
    base = auto_disable._custom_nodes_dir()
    direct = os.path.join(base, module_name)
    if os.path.isdir(direct):
        return os.path.abspath(direct)
    file_py = os.path.join(base, module_name + ".py")
    if os.path.isfile(file_py):
        return os.path.abspath(file_py)
    return ""


def extract_node_classes_from_path(path: str) -> Optional[list[str]]:
    """安全地从某个已禁用模块路径里提取 ``NODE_CLASS_MAPPINGS`` 的 key 列表。

    调用场景：reconcile 时发现磁盘上有但 state 里没有的模块；为了后续的
    ``restore_for_missing_classes`` 能按类名匹配，需要知道该模块提供了哪些类。

    实现要点
    --------

    - 仅在 ``.disabled/<name>`` 目录或单 ``.py`` 文件存在时尝试。
    - 使用 :mod:`importlib.util` 以文件路径方式加载（不依赖 ``sys.path`` 顺序）。
    - **严格捕获所有异常**——导入、副作用、语法错误、缺失依赖一律返回 ``None``，
      绝不向 reconcile 流程抛出。
    - 仅读取顶层 ``__init__.py``（目录形态）或单 ``.py``；不递归子包，避免
      装载面失控。
    - 读取 ``NODE_CLASS_MAPPINGS`` 时仅接受 dict；其它形态视为未提供。

    返回排序后的类名列表，失败时返回 ``None``（语义区别于空列表——后者表示
    模块加载成功但未声明节点映射）。
    """
    if not path or not os.path.exists(path):
        return None
    try:
        import importlib.util as _ilu

        if os.path.isdir(path):
            init_py = os.path.join(path, "__init__.py")
            if not os.path.isfile(init_py):
                return None
            target = init_py
        elif os.path.isfile(path) and path.endswith(".py"):
            target = path
        else:
            return None

        # 使用带 id 后缀的合成模块名，避免冲突且不会污染真实模块名空间
        mod_name = (
            f"_auto_disable_scan_{os.path.basename(path)}_{id(path)}"
        )
        spec = _ilu.spec_from_file_location(mod_name, target)
        if spec is None or spec.loader is None:
            return None

        module = _ilu.module_from_spec(spec)
        # 把合成模块注册到 sys.modules，让模块内部 import 能找到自己
        sys.modules.setdefault(mod_name, module)
        try:
            spec.loader.exec_module(module)
        except Exception:
            # 导入期任何异常（依赖缺失、语法错、副作用报错）一律吞掉
            return None

        mapping = getattr(module, "NODE_CLASS_MAPPINGS", None)
        if isinstance(mapping, dict) and mapping:
            return sorted(str(k) for k in mapping.keys() if k)
        # exec_module 成功：无论是否声明了 NODE_CLASS_MAPPINGS，都按“已加载、
        # 但未提供映射”处理，返回空列表。仅 exec_module 抛异常时才返回 None。
        return []
    except Exception:
        return None