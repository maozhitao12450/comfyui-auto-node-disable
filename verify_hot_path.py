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
    """清空状态文件、disabled 目录，把模块从稳定备份还原。

    Windows 下 SQLite 进程句柄可能还持有 db 文件，``unlink`` 会失败
    （PermissionError）；更可靠的做法是直接打开连接并清空所有业务表。
    """
    p = Path(auto_disable._state_path())
    import sqlite3
    try:
        with sqlite3.connect(str(p)) as conn:
            for tbl in (
                "settings",
                "known_modules",
                "rounds",
                "disabled",
                "pending_restart",
            ):
                conn.execute(f"DELETE FROM {tbl}")
            conn.commit()
    except sqlite3.OperationalError:
        # db 文件还不存在：下次 ``_save_state`` 会重建空 schema
        pass
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

    # 重置启动一次性扫描标志：_STARTUP_SCAN_DONE 是模块级全局变量，
    # 跨 scenario 共享。每个 scenario 都应在自己的 _load_state 中触发启动扫描，
    # 否则后续 scenario 看到的 known_modules 是空、_decide 直接 return，
    # 验证场景将不再贴合真实启动语义。
    auto_disable.reset_startup_scan_flag()


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
# Scenario 6: known_modules 与 disable/restore 同步
#   - 启动一次性扫描：仅首次 _load_state 调用 _scan_known_modules
#   - 物理 disable 后从 known_modules 移除
#   - restore_for_missing_classes 恢复后回填 known_modules
# ---------------------------------------------------------------------------
def scenario_known_modules_sync() -> None:
    _scenario("known_modules stays in sync with disable / restore")
    _reset_state()
    auto_disable.set_threshold(2)
    auto_disable.set_dry_run(False)

    # 1) 验证启动一次性扫描：监控 _fake_scan 调用次数。
    # 跨 scenario 共享进程，_STARTUP_SCAN_DONE 可能已被前序 scenario 置 True；
    # 这里重置以观察“本场景内的多次 _load_state 是否只触发一次扫描”。
    auto_disable.reset_startup_scan_flag()
    auto_disable._scan_known_modules = _fake_scan  # type: ignore[assignment]

    scan_calls = {"count": 0}

    def counting_scan(state):
        scan_calls["count"] += 1
        return _fake_scan(state)

    auto_disable._scan_known_modules = counting_scan  # type: ignore[assignment]
    # 连续多次 _load_state：只应扫描一次
    auto_disable._load_state()
    auto_disable._load_state()
    auto_disable._load_state()
    _assert(
        scan_calls["count"] == 1,
        f"startup scan should fire exactly once per process, got {scan_calls['count']}",
    )

    # 2) 验证启动扫描结果落盘：known_modules 含两个候选模块
    snap = auto_disable.snapshot()
    _assert(
        set(snap["known_modules"].keys()) == {"module_a", "module_b"},
        f"after startup scan, known_modules should contain module_a/module_b, "
        f"got {set(snap['known_modules'].keys())}",
    )

    # 3) 物理 disable 后 known_modules 应移除该模块
    auto_disable.record_prompt({"prompt": {"1": {"class_type": "ClassA"}}}, prompt_id="p-sync-1")
    auto_disable.record_prompt(["OtherClass"], prompt_id="p-sync-2")
    # 第 3 轮触发 module_a 禁用（threshold=2 时 recent=[OtherClass, ClassA],
    # 但 _decide 看 module_a 的 node_classes=[ClassA] 与 used_union={ClassA, OtherClass}
    # 有交集，不会禁用。要让 module_a 禁用，需要 recent 不含 ClassA）
    # 用 None 作为本轮的 used，跳过 _decide 决策
    # 重新做：threshold=1，让第 2 轮（不含 ClassA）就能触发 module_a 禁用
    auto_disable.set_threshold(1)
    auto_disable.record_prompt(["OtherClass"], prompt_id="p-sync-3")
    auto_disable.record_prompt(["AnotherClass"], prompt_id="p-sync-4")

    snap = auto_disable.snapshot()
    _assert(
        "module_a" in snap["disabled"],
        "module_a should be physically disabled after consecutive non-ClassA rounds",
    )
    _assert(
        "module_a" not in snap["known_modules"],
        "physically disabled module_a should be removed from known_modules",
    )
    _assert(
        (DISABLED_DIR / "module_a").exists(),
        "module_a should have been moved to .disabled/",
    )

    # 4) restore_for_missing_classes 后 known_modules 应回填该模块
    # 模拟当前 NODE_CLASS_MAPPINGS 不含 ClassA → trigger 自动恢复
    real_registered = auto_disable._current_registered_classes
    auto_disable._current_registered_classes = lambda: {"OtherClass"}  # type: ignore[assignment]
    try:
        # 用 ClassA 触发 restore
        auto_disable.record_prompt({"prompt": {"1": {"class_type": "ClassA"}}}, prompt_id="p-sync-5")
    finally:
        auto_disable._current_registered_classes = real_registered  # type: ignore[assignment]

    snap = auto_disable.snapshot()
    _assert(
        "module_a" in snap["known_modules"],
        "auto-restored module_a should be added back to known_modules",
    )
    _assert(
        "ClassA" in snap["known_modules"]["module_a"]["node_classes"],
        "known_modules should record node_classes of restored module",
    )


# ---------------------------------------------------------------------------
# Scenario 7: refresh_known_modules 运行时手动刷新
# ---------------------------------------------------------------------------
def scenario_refresh_known_modules() -> None:
    _scenario("refresh_known_modules can rescan on demand at runtime")
    _reset_state()
    auto_disable.set_threshold(3)
    auto_disable.set_dry_run(False)

    # 1) 第一次 _load_state 用默认 _fake_scan 建立 known_modules。
    # 重置标志使本次 _load_state 能真正执行启动扫描（前序 scenario 可能已置 True）。
    auto_disable.reset_startup_scan_flag()
    auto_disable._scan_known_modules = _fake_scan  # type: ignore[assignment]
    auto_disable._load_state()
    snap = auto_disable.snapshot()
    _assert(
        set(snap["known_modules"].keys()) == {"module_a", "module_b"},
        "initial known_modules should contain module_a/module_b",
    )

    # 2) 临时改 _fake_scan 模拟“运行时装了新模块”：
    new_scan_result = {
        "module_a": {"node_classes": ["ClassA"], "module_path": str(CUSTOM_NODES / "module_a")},
        "module_b": {"node_classes": ["ClassB"], "module_path": str(CUSTOM_NODES / "module_b")},
        "newly_installed": {
            "node_classes": ["NewlyInstalledClass"],
            "module_path": str(CUSTOM_NODES / "newly_installed"),
        },
    }

    def enriched_scan(state):
        state["known_modules"] = {
            name: {"node_classes": list(info["node_classes"]), "module_path": info["module_path"]}
            for name, info in new_scan_result.items()
        }
        return state["known_modules"]

    auto_disable._scan_known_modules = enriched_scan  # type: ignore[assignment]

    # 3) 调用 refresh_known_modules
    view = auto_disable.refresh_known_modules()
    _assert(
        "newly_installed" in view,
        "refresh_known_modules view should include the newly installed module",
    )

    # 4) 持久化生效：snapshot 也能看到
    snap = auto_disable.snapshot()
    _assert(
        "newly_installed" in snap["known_modules"],
        "refresh_known_modules should persist the new scan result",
    )
    _assert(
        set(snap["known_modules"].keys()) == {"module_a", "module_b", "newly_installed"},
        "snapshot should reflect the enriched known_modules after refresh",
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
        scenario_known_modules_sync()
        scenario_refresh_known_modules()
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