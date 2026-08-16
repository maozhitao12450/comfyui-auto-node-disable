"""聚焦门禁脚本：变更生命周期最小验证入口。

用法：
    python tools/change_check.py            # 默认：基线校验（语法 + 公共面）
    python tools/change_check.py --record   # 校验并把结果追加到 tools/check_log.jsonl

退出码：
    0  通过
    1  失败（语法/签名/必填 API 缺失）
    2  使用错误

设计约束
========
- 不导入产品模块（避免触发 ``from nodes import NODE_CLASS_MAPPINGS`` 等
  ComfyUI 宿主依赖），只做静态检查；保证在 CI/本地均可运行。
- 每次运行把一行 JSON 结果追加到 ``tools/check_log.jsonl``（修订级验收记录）：
  ``{"timestamp": ..., "commit": ..., "status": "pass|fail", "summary": ...}``
- 只读产品文件，不修改任何业务文件。
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = PROJECT_ROOT / "tools"
LOG_PATH = TOOLS_DIR / "check_log.jsonl"

# 必填 API（项目对外/对内契约），缺失即失败
REQUIRED_AUTO_DISABLE_API = {
    "record_prompt",
    "restore_module",
    "set_threshold",
    "set_exclude",
    "snapshot",
    "extract_used_class_names",
    "DEFAULT_THRESHOLD",
    "DEFAULT_EXCLUDE",
    "STATE_FILENAME",
}

# 不应被悄悄改动的热路径（仅做存在性核对；不做内容匹配）
REQUIRED_HOT_PATH_NAMES = {"_disable_module", "_decide", "_save_state"}

# 解析/扫描排除项
EXCLUDE_DIR_PARTS = {".git", "__pycache__", ".qoder", "tools/.check_cache"}
EXCLUDE_JSON_NAMES = {"auto_node_disable_state.json"}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _walk_py_files(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        # 原地裁剪以跳过
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIR_PARTS]
        for fn in filenames:
            if fn.endswith(".py"):
                yield Path(dirpath) / fn


def _walk_json_files(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIR_PARTS]
        for fn in filenames:
            if not fn.endswith(".json"):
                continue
            if fn in EXCLUDE_JSON_NAMES:
                continue
            yield Path(dirpath) / fn


def _current_commit() -> str:
    """返回 ``git rev-parse HEAD``，非仓库或失败时返回 ``"uncommitted"``。"""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(PROJECT_ROOT),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return out or "uncommitted"
    except Exception:
        return "uncommitted"


# ---------------------------------------------------------------------------
# 检查项
# ---------------------------------------------------------------------------


def check_python_syntax() -> list[str]:
    errors: list[str] = []
    for py in _walk_py_files(PROJECT_ROOT):
        try:
            with open(py, "rb") as f:
                ast.parse(f.read(), filename=str(py))
        except SyntaxError as e:
            errors.append(f"{py.relative_to(PROJECT_ROOT)}: SyntaxError: {e.msg} (line {e.lineno})")
        except Exception as e:
            errors.append(f"{py.relative_to(PROJECT_ROOT)}: parse failed: {e}")
    return errors


def check_json_syntax() -> list[str]:
    errors: list[str] = []
    for js in _walk_json_files(PROJECT_ROOT):
        try:
            with open(js, "r", encoding="utf-8") as f:
                json.load(f)
        except Exception as e:
            errors.append(f"{js.relative_to(PROJECT_ROOT)}: invalid JSON: {e}")
    return errors


def _collect_defined_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """从一个已解析的 AST 中提取函数名与赋值名。"""
    defined_funcs = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    defined_assigns: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    defined_assigns.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined_assigns.add(node.target.id)
    return defined_funcs, defined_assigns


def _resolve_auto_disable_targets() -> list[Path]:
    """返回要检查的 auto_disable 源文件。

    - 优先单文件 ``auto_disable.py``；
    - 若不存在则回退到 package 形式：返回 ``auto_disable/__init__.py``
      以及同目录下所有子模块 ``_*.py``，覆盖拆分后的形态。
    """
    legacy = PROJECT_ROOT / "auto_disable.py"
    if legacy.exists():
        return [legacy]
    pkg_dir = PROJECT_ROOT / "auto_disable"
    if not pkg_dir.is_dir():
        return []
    targets: list[Path] = []
    init_py = pkg_dir / "__init__.py"
    if init_py.exists():
        targets.append(init_py)
    for sub in sorted(pkg_dir.glob("_*.py")):
        if sub.name == "__init__.py":
            continue
        targets.append(sub)
    return targets


def check_required_api() -> list[str]:
    """检查 ``auto_disable`` 是否仍导出关键符号。

    支持单文件 ``auto_disable.py`` 或 package 形式
    ``auto_disable/__init__.py``；后者只检查 ``__init__.py`` 顶层
    ``__all__`` 与 ``ast.Assign`` 出的名字，因为这是子模块符号对外
    重新绑定的入口。
    """
    targets = _resolve_auto_disable_targets()
    if not targets:
        return ["missing required file: auto_disable.py or auto_disable/__init__.py"]

    defined_funcs: set[str] = set()
    defined_assigns: set[str] = set()
    for target in targets:
        try:
            tree = ast.parse(target.read_text(encoding="utf-8"), filename=str(target))
        except SyntaxError as e:
            return [f"{target.relative_to(PROJECT_ROOT)}: SyntaxError: {e.msg}"]
        f, a = _collect_defined_names(tree)
        defined_funcs |= f
        defined_assigns |= a

    missing = []
    for name in REQUIRED_AUTO_DISABLE_API:
        if name in defined_funcs or name in defined_assigns:
            continue
        missing.append(name)
    if missing:
        return [f"auto_disable: missing required symbols: {sorted(missing)}"]
    return []


def check_hot_paths_present() -> list[str]:
    """热路径（高风险副作用所在函数）必须存在；缺则视为破坏。

    同时检查 ``defined_funcs`` 与 ``defined_assigns``，覆盖以下两种场景：
    - 单文件 ``auto_disable.py``：热路径作为函数定义存在；
    - package 拆分后：热路径在子模块里以无下划线名定义，再由
      ``__init__.py`` 通过 ``_decide = _decision.decide`` 形式重新
      绑定为 ``_decide``，此时它是 ``Assign`` 的 target。
    """
    targets = _resolve_auto_disable_targets()
    if not targets:
        return []

    defined_funcs: set[str] = set()
    defined_assigns: set[str] = set()
    for target in targets:
        try:
            tree = ast.parse(target.read_text(encoding="utf-8"), filename=str(target))
        except SyntaxError:
            return []  # 已被语法检查覆盖
        f, a = _collect_defined_names(tree)
        defined_funcs |= f
        defined_assigns |= a

    present = defined_funcs | defined_assigns
    missing = sorted(REQUIRED_HOT_PATH_NAMES - present)
    if missing:
        return [f"hot-path functions missing in auto_disable: {missing}"]
    return []


def check_js_syntax() -> list[str]:
    """对 ``web/js/*.js`` 做轻量校验：能 ``node --check`` 才算通过。

    若无 ``node`` 可用，跳过而不算失败（不强绑外部工具链）。
    """
    js_dir = PROJECT_ROOT / "web" / "js"
    if not js_dir.is_dir():
        return []
    node = "node"
    errors: list[str] = []
    for js in sorted(js_dir.glob("*.js")):
        try:
            r = subprocess.run(
                [node, "--check", str(js)],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except FileNotFoundError:
            # 没有 node，跳过
            return []
        except Exception as e:
            errors.append(f"{js.relative_to(PROJECT_ROOT)}: node check errored: {e}")
            continue
        if r.returncode != 0:
            err = (r.stderr or r.stdout).strip().splitlines()
            errors.append(
                f"{js.relative_to(PROJECT_ROOT)}: JS syntax error: {err[0] if err else 'unknown'}"
            )
    return errors


# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------


def _print_section(title: str) -> None:
    print(f"\n== {title} ==")


def run_checks() -> tuple[bool, list[str], list[str]]:
    """返回 ``(ok, errors, warnings)``。"""
    errors: list[str] = []
    warnings: list[str] = []

    _print_section("python syntax")
    e = check_python_syntax()
    if e:
        errors.extend(e)
    print(f"  {'FAIL' if e else 'PASS'}  ({len(e)} issues)")

    _print_section("json syntax")
    e = check_json_syntax()
    if e:
        errors.extend(e)
    print(f"  {'FAIL' if e else 'PASS'}  ({len(e)} issues)")

    _print_section("required API surface (auto_disable)")
    e = check_required_api()
    if e:
        errors.extend(e)
    print(f"  {'FAIL' if e else 'PASS'}  ({len(e)} issues)")

    _print_section("hot-path functions present")
    e = check_hot_paths_present()
    if e:
        errors.extend(e)
    print(f"  {'FAIL' if e else 'PASS'}  ({len(e)} issues)")

    _print_section("frontend js syntax (best-effort)")
    e = check_js_syntax()
    if e:
        warnings.extend(e)  # JS 错误视为 warning，不阻塞门禁
    print(f"  {'FAIL' if e else 'PASS'}  ({len(e)} issues)")

    return (not errors), errors, warnings


def append_log(ok: bool, errors: list[str], warnings: list[str]) -> None:
    """修订级验收记录：每次运行追加一行 JSON。"""
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "timestamp": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "commit": _current_commit(),
        "status": "pass" if ok else "fail",
        "errors": errors,
        "warnings": warnings,
    }
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="聚焦门禁入口")
    parser.add_argument(
        "--record",
        action="store_true",
        help="把本次结果追加到 tools/check_log.jsonl（验收记录）",
    )
    args = parser.parse_args(argv)

    ok, errors, warnings = run_checks()

    _print_section("summary")
    print(f"  status: {'PASS' if ok else 'FAIL'}")
    print(f"  errors: {len(errors)}, warnings: {len(warnings)}")

    if args.record:
        try:
            append_log(ok, errors, warnings)
            print(f"  recorded -> {LOG_PATH.relative_to(PROJECT_ROOT)}")
        except Exception as e:
            print(f"  WARN: failed to append log: {e}", file=sys.stderr)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
