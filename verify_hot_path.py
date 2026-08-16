"""
verify_hot_path.py
==================
对热路径自动禁用的最小安全边界做聚焦检查：

1. 干跑 (dry-run)   状态写入 ``disabled`` 条目但目录不动
2. 保存失败          禁用记录要么 pending 后回滚，要么 confirmed 与目录一致
3. 重复入队          同一模块只产生一条 confirmed 条目，不会重复移动
4. 恢复入口          ``restore_module`` 能把目录搬回并清理状态
5. 启动对齐          遗留的 pending 记录按文件存在与否自动收敛

运行方式（依赖纯标准库，可脱离 ComfyUI 宿主）：

    python verify_hot_path.py

退出码：0 = 全部通过；非 0 = 至少一个断言失败。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

# ---------------------------------------------------------------------------
# 让脚本可在项目根目录直接运行
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 静音 auto_disable 的 warning 噪声，让断言输出更干净
logging.getLogger("auto_node_disable").setLevel(logging.ERROR)


# ---------------------------------------------------------------------------
# 构造隔离的临时工作目录（不污染真实 custom_nodes）
# ---------------------------------------------------------------------------
TEMP = Path(tempfile.mkdtemp(prefix="auto_disable_verify_"))
os.environ["COMFYUI_PATH"] = str(TEMP)

CUSTOM_NODES = TEMP / "custom_nodes"
CUSTOM_NODES.mkdir()
DISABLED_DIR = CUSTOM_NODES / ".disabled"

# 用稳定的备份作为后续 _reset_state 的还原源，避免模块被移走后无法复用
BACKUP_DIR = TEMP / "_backups"
BACKUP_DIR.mkdir()
BACKUP_A = BACKUP_DIR / "module_a"
BACKUP_B = BACKUP_DIR / "module_b"


def _make_module(name: str) -> Path:
    p = BACKUP_DIR / name
    p.mkdir()
    (p / "__init__.py").write_text("# fake module for verification\n", encoding="utf-8")
    return p


MODULE_A = _make_module("module_a")
MODULE_B = _make_module("module_b")
# 把 backup 也复制到 custom_nodes 下，让一开始两个模块都"在场"
for name, src in (("module_a", MODULE_A), ("module_b", MODULE_B)):
    dst = CUSTOM_NODES / name
    shutil.copytree(src, dst)


# ---------------------------------------------------------------------------
# 让 ``auto_disable`` 把路径解析指向临时目录
# ---------------------------------------------------------------------------
import auto_disable  # noqa: E402

auto_disable._custom_nodes_dir = lambda: str(CUSTOM_NODES)  # type: ignore[assignment]
auto_disable._comfy_root = lambda: str(TEMP)  # type: ignore[assignment]
auto_disable._disabled_dir = lambda: str(DISABLED_DIR)  # type: ignore[assignment]
auto_disable._state_path = lambda: str(TEMP / auto_disable.STATE_FILENAME)  # type: ignore[assignment]


def _fake_scan(state):
    """跳过对 ComfyUI 全局映射的依赖，直接喂入两个候选模块。"""
    state["known_modules"] = {
        "module_a": {"node_classes": ["ClassA"], "module_path": str(CUSTOM_NODES / "module_a")},
        "module_b": {"node_classes": ["ClassB"], "module_path": str(CUSTOM_NODES / "module_b")},
    }
    return state["known_modules"]


auto_disable._scan_known_modules = _fake_scan  # type: ignore[assignment]


def _fake_resolve(name: str) -> str:
    """绕过 import 限制，直接返回 custom_nodes/<name> 路径。"""
    p = CUSTOM_NODES / name
    if p.exists():
        return str(p)
    f = CUSTOM_NODES / (name + ".py")
    return str(f) if f.exists() else ""


auto_disable._resolve_module_path = _fake_resolve  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------

FAILED: list[tuple[str, str]] = []


def _scenario(name: str) -> None:
    print()
    print("=" * 64)
    print(f"Scenario: {name}")
    print("=" * 64)


def _assert(cond: bool, msg: str) -> None:
    if cond:
        print(f"  [PASS] {msg}")
    else:
        print(f"  [FAIL] {msg}")
        FAILED.append((msg, traceback.format_stack()[-2]))


def _reset_state() -> None:
    """清空状态文件、disabled 目录，把模块从稳定备份还原。"""
    p = Path(auto_disable._state_path())
    if p.exists():
        p.unlink()
    if DISABLED_DIR.exists():
        shutil.rmtree(DISABLED_DIR)
    DISABLED_DIR.mkdir()
    for name, backup in (("module_a", BACKUP_A), ("module_b", BACKUP_B)):
        target = CUSTOM_NODES / name
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        shutil.copytree(backup, target)


# ---------------------------------------------------------------------------
# Scenario 1: 干跑模式只写审计，不动目录
# ---------------------------------------------------------------------------
def scenario_dry_run() -> None:
    _scenario("dry-run only records disabled entries, never moves")
    _reset_state()
    auto_disable.set_threshold(2)
    auto_disable.set_dry_run(True)

    # 第 1 轮使用 ClassA，让 module_a 被认为“用过”；
    # 第 2/3/4/5 轮不命中。第 5 轮的窗口为 [Other, Another, Yet, More]，
    # 全部不包含 ClassA，触发 module_a 进入干跑审计，prompt_id 应为 p-dry-5。
    auto_disable.set_threshold(4)
    auto_disable.record_prompt({"prompt": {"1": {"class_type": "ClassA"}}}, prompt_id="p-dry-1")
    auto_disable.record_prompt(["OtherClass"], prompt_id="p-dry-2")
    auto_disable.record_prompt(["AnotherClass"], prompt_id="p-dry-3")
    auto_disable.record_prompt(["YetAnother"], prompt_id="p-dry-4")
    auto_disable.record_prompt(["MoreOther"], prompt_id="p-dry-5")

    snap = auto_disable.snapshot()
    _assert(
        "module_a" in snap["disabled"],
        "module_a should be recorded under disabled (dry-run)",
    )
    _assert(
        snap["disabled"].get("module_a", {}).get("status") == "dry_run",
        "module_a should have status='dry_run'",
    )
    _assert(
        snap["disabled"].get("module_a", {}).get("prompt_id") == "p-dry-5",
        "module_a should record the prompt_id that triggered the decision",
    )
    _assert(
        (CUSTOM_NODES / "module_a").exists(),
        "module_a directory must still exist under custom_nodes",
    )
    _assert(
        not (DISABLED_DIR / "module_a").exists(),
        "module_a must NOT be moved under dry-run",
    )


# ---------------------------------------------------------------------------
# Scenario 2: 保存失败 → 状态与目录保持一致（移动不发生）
# ---------------------------------------------------------------------------
def scenario_save_failure() -> None:
    _scenario("save failure mid-decide → state and directory stay consistent")
    _reset_state()
    auto_disable.set_threshold(3)
    auto_disable.set_dry_run(False)

    real_atomic = auto_disable._atomic_write_json

    def flaky_atomic(path, data):
        # 只让“包含 pending 条目”的那次保存失败，这样不会干扰到 round 保存
        disabled = data.get("disabled") or {}
        has_pending = any(
            isinstance(v, dict) and v.get("status") == "pending"
            for v in disabled.values()
        )
        if has_pending:
            raise IOError("simulated: disk full while persisting pending")
        real_atomic(path, data)

    auto_disable._atomic_write_json = flaky_atomic  # type: ignore[assignment]
    try:
        # 第一轮使用 ClassA，避免 module_a 被考虑；第二轮与第三轮不命中 ClassA/ClassB
        auto_disable.record_prompt({"prompt": {"1": {"class_type": "ClassA"}}}, prompt_id="p-save-1")
        auto_disable.record_prompt(["OtherClass"], prompt_id="p-save-2")
        auto_disable.record_prompt(["AnotherClass"], prompt_id="p-save-3")
    except Exception as e:
        # 原子化回滚应该把异常吞掉，record_prompt 不应让 IOError 冒泡
        print(f"  unexpected exception escaped: {e}")
        FAILED.append(("save failure should not propagate out of record_prompt", str(e)))
    auto_disable._atomic_write_json = real_atomic  # type: ignore[assignment]

    snap = auto_disable.snapshot()
    # 关键断言：module_b 不应在 disabled 中（pending 已回滚），且目录未被移动
    _assert(
        "module_b" not in snap["disabled"],
        "module_b should NOT remain as orphan after pending save failure",
    )
    _assert(
        (CUSTOM_NODES / "module_b").exists(),
        "module_b must still exist under custom_nodes after rollback",
    )
    _assert(
        not (DISABLED_DIR / "module_b").exists(),
        "module_b must NOT be moved under .disabled after save failure",
    )


# ---------------------------------------------------------------------------
# Scenario 3: 重复入队只产生一条 confirmed 条目
# ---------------------------------------------------------------------------
def scenario_duplicate_enqueue() -> None:
    _scenario("duplicate enqueue → only one confirmed disable entry")
    _reset_state()
    auto_disable.set_threshold(2)
    auto_disable.set_dry_run(False)

    # 第 1 轮使用 ClassA → module_a 被认为"用过"
    auto_disable.record_prompt({"prompt": {"1": {"class_type": "ClassA"}}}, prompt_id="p-dup-1")
    # 第 2/3/4 轮都不使用 ClassA → 第 3 轮触发 module_a 禁用
    auto_disable.record_prompt(["OtherClass"], prompt_id="p-dup-2")
    auto_disable.record_prompt(["AnotherClass"], prompt_id="p-dup-3")
    auto_disable.record_prompt(["YetAnother"], prompt_id="p-dup-4")
    auto_disable.record_prompt(["MoreOther"], prompt_id="p-dup-5")

    snap = auto_disable.snapshot()
    ma_entry = snap["disabled"].get("module_a", {})
    _assert(
        ma_entry.get("status") == "confirmed",
        "module_a should have status='confirmed'",
    )
    _assert(
        ma_entry.get("prompt_id") == "p-dup-3",
        f"module_a should record prompt_id='p-dup-3', got {ma_entry.get('prompt_id')!r}",
    )
    _assert(
        not (CUSTOM_NODES / "module_a").exists(),
        "module_a should have been moved out of custom_nodes",
    )
    _assert(
        (DISABLED_DIR / "module_a").exists(),
        "module_a should now live under .disabled/",
    )
    # 重复入队的关键：即使再多轮也不会让 module_a 二次移动
    # module_a 应仍只在 disabled 中出现一次
    confirmed_count = sum(
        1 for k, v in snap["disabled"].items()
        if k.startswith("module_") and v.get("status") == "confirmed"
    )
    _assert(
        confirmed_count == 2,
        f"expected 2 confirmed entries (module_a + module_b), got {confirmed_count}",
    )


# ---------------------------------------------------------------------------
# Scenario 4: restore_module 能把目录搬回并清理状态
# ---------------------------------------------------------------------------
def scenario_restore() -> None:
    _scenario("restore_module reverses disable and cleans state")
    # 复用 Scenario 3 留下的禁用状态
    snap_before = auto_disable.snapshot()
    _assert(
        "module_a" in snap_before["disabled"],
        "module_a should still be in disabled before restore",
    )

    ok = auto_disable.restore_module("module_a")
    _assert(ok is True, "restore_module('module_a') should return True")
    _assert(
        (CUSTOM_NODES / "module_a").exists(),
        "module_a should be back under custom_nodes",
    )
    _assert(
        not (DISABLED_DIR / "module_a").exists(),
        ".disabled/module_a should be gone after restore",
    )

    snap_after = auto_disable.snapshot()
    _assert(
        "module_a" not in snap_after["disabled"],
        "module_a should be removed from disabled map",
    )


# ---------------------------------------------------------------------------
# Scenario 5: 启动时遗留 pending 按文件存在与否收敛
# ---------------------------------------------------------------------------
def scenario_startup_reconcile() -> None:
    _scenario("startup reconciliation aligns pending records with reality")

    # 5a. pending + 原路径仍在 → 应被回滚
    _reset_state()
    auto_disable._save_state({
        "threshold": 3,
        "dry_run": False,
        "exclude": list(auto_disable.DEFAULT_EXCLUDE),
        "known_modules": {
            "module_a": {"node_classes": ["ClassA"], "module_path": str(CUSTOM_NODES / "module_a")},
        },
        "rounds": [],
        "disabled": {
            "module_a": {
                "original_path": str(CUSTOM_NODES / "module_a"),
                "disabled_at": time.time(),
                "prompt_id": "p-stale-1",
                "status": "pending",
            }
        },
    })
    snap = auto_disable.snapshot()
    _assert(
        "module_a" not in snap["disabled"],
        "stale pending (path still exists) should be rolled back on load",
    )

    # 5b. pending + 原路径已不在 → 应被标记 confirmed
    _reset_state()
    # 模拟“移动已发生但还没来得及写 confirmed”：先把 module_b 移走
    shutil.move(str(CUSTOM_NODES / "module_b"), str(DISABLED_DIR / "module_b"))
    auto_disable._save_state({
        "threshold": 3,
        "dry_run": False,
        "exclude": list(auto_disable.DEFAULT_EXCLUDE),
        "known_modules": {
            "module_b": {"node_classes": ["ClassB"], "module_path": str(CUSTOM_NODES / "module_b")},
        },
        "rounds": [],
        "disabled": {
            "module_b": {
                "original_path": str(CUSTOM_NODES / "module_b"),
                "disabled_at": time.time(),
                "prompt_id": "p-confirmed-1",
                "status": "pending",
            }
        },
    })
    snap = auto_disable.snapshot()
    _assert(
        "module_b" in snap["disabled"],
        "module_b should still be in disabled after reconcile",
    )
    _assert(
        snap["disabled"]["module_b"].get("status") == "confirmed",
        "pending record whose path is gone should be promoted to confirmed",
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        scenario_dry_run()
        scenario_save_failure()
        scenario_duplicate_enqueue()
        scenario_restore()
        scenario_startup_reconcile()
    except Exception:
        print("UNEXPECTED EXCEPTION:")
        traceback.print_exc()
        return 2
    finally:
        # 清理临时目录
        shutil.rmtree(TEMP, ignore_errors=True)

    print()
    print("=" * 64)
    if FAILED:
        print(f"FAILED ({len(FAILED)} assertion(s)):")
        for msg, stack in FAILED:
            print(f"  - {msg}")
        return 1
    print("ALL SCENARIOS PASSED")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())