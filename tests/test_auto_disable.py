"""auto_disable 最小测试网格。

================================================================
运行方式
================================================================

在仓库根目录下任选其一：

    # 方式 A：pytest（推荐，自动发现 ``tests/`` 下的所有测试）
    python -m pytest tests/ -v

    # 方式 B：unittest（无第三方依赖）
    python -m unittest tests.test_auto_disable -v

    # 方式 C：直接执行（仅运行本文件时可用）
    python -m tests.test_auto_disable

================================================================
覆盖范围
================================================================

四个用户指定的关键判定维度：
  1. 阈值判定（threshold=0 / -1 / 1 / 3 + exclude + dry_run）
  2. 轮次不足（rounds 数量 < threshold 时不触发决策）
  3. 恢复回退（_disable_module / restore_module 的异常路径与 roundtrip）
  4. 窗口修剪（rounds 超 keep*4 时被截断）
  5. 缺失节点自动恢复（submit 引用未注册类 → .disabled 匹配 → 原子恢复 →
     pending_restart 提示与消费；含拒绝/忽略/原子化等边界）

并补全了产品当前实现的额外安全边界：
  - dry-run 干跑模式
  - 三步原子化的 pending → confirmed / rollback
  - 启动时 _reconcile_pending 对齐 pending 状态与文件位置
  - 重复入队与 prompt_id 审计字段

================================================================
设计原则
================================================================

- 仅修改测试文件（``tests/``、必要时 ``pyproject.toml`` 加测试配置），
  不改动产品代码（``auto_disable.py``、``__init__.py``）。
- 通过 ``unittest.mock`` 重写模块级路径函数，避开对真实 ComfyUI 的依赖。
- 每个测试用临时目录隔离文件系统副作用，互不污染。
- ``dry_run=True`` 用于纯决策逻辑测试，避免依赖真实磁盘移动；
  ``record_prompt`` 集成测试用 ``dry_run=False`` + 真实模块目录。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import auto_disable  # noqa: E402


# ---------------------------------------------------------------------------
# 共享基类：替换所有模块级路径与扫描副作用，绑定到临时目录
# ---------------------------------------------------------------------------


class _IsolatedTestBase(unittest.TestCase):
    """把 auto_disable 的所有磁盘路径重定向到 tmpdir，并屏蔽 ``nodes`` 导入。"""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="auto_disable_test_")
        self.custom_nodes_dir = os.path.join(self.tmpdir, "custom_nodes")
        self.disabled_dir = os.path.join(self.custom_nodes_dir, ".disabled")
        self.state_file = os.path.join(self.tmpdir, "auto_node_disable_state.json")
        os.makedirs(self.custom_nodes_dir, exist_ok=True)

        self._patches = [
            mock.patch.object(
                auto_disable, "_state_path", return_value=self.state_file
            ),
            mock.patch.object(
                auto_disable, "_custom_nodes_dir", return_value=self.custom_nodes_dir
            ),
            mock.patch.object(
                auto_disable, "_disabled_dir", return_value=self.disabled_dir
            ),
            mock.patch.object(auto_disable, "_comfy_root", return_value=self.tmpdir),
            mock.patch.object(
                auto_disable, "_scan_known_modules", side_effect=lambda s: s
            ),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ----- 工具方法 -----

    @staticmethod
    def _keep(threshold: int) -> int:
        """产品代码里 ``keep = max(threshold + 2, 5)`` 的镜像。"""
        return max(int(threshold) + 2, 5)

    def _make_module(self, name: str, contents: str = "# marker") -> str:
        """在 custom_nodes/ 下创建模块目录并写入标记文件，返回模块路径。"""
        mod_path = os.path.join(self.custom_nodes_dir, name)
        os.makedirs(mod_path, exist_ok=True)
        with open(os.path.join(mod_path, "marker.py"), "w", encoding="utf-8") as f:
            f.write(contents)
        return mod_path

    def _state(self, threshold=3, rounds=None, exclude=None, known=None):
        """构造普通状态：``known_modules`` 用空路径（决策逻辑测试用）。"""
        s = auto_disable._default_state()
        s["threshold"] = threshold
        s["rounds"] = list(rounds or [])
        s["exclude"] = list(exclude or [])
        s["known_modules"] = self._known(known) if known else {}
        return s

    def _dry_state(self, threshold=3, rounds=None, exclude=None, known=None):
        """开启 dry_run 的状态：``_decide`` 只写审计字段、不移动目录。"""
        s = self._state(threshold, rounds, exclude, known)
        s["dry_run"] = True
        return s

    def _known(self, mapping):
        """把 ``{name: [classes]}`` 转成 known_modules 形态（路径为空）。"""
        return {
            name: {"node_classes": list(classes), "module_path": ""}
            for name, classes in mapping.items()
        }

    def _round(self, *used_classes, ts=0.0):
        return {"timestamp": ts, "used_classes": sorted(set(used_classes))}


# ---------------------------------------------------------------------------
# 1. 阈值判定
# ---------------------------------------------------------------------------


class ThresholdJudgmentTests(_IsolatedTestBase):
    """阈值边界：threshold=0、负数、1、3 + exclude + dry_run。"""

    def test_threshold_zero_disables_nothing(self):
        """threshold=0 时，``_decide`` 应直接返回空，永不触发禁用。"""
        s = self._dry_state(
            threshold=0,
            rounds=[self._round("A1") for _ in range(10)],
            known={"mod_a": ["A1"]},
        )
        self.assertEqual(auto_disable._decide(s), [])

    def test_threshold_negative_disables_nothing(self):
        """threshold=-1（负数）也属于非法值，应直接返回空。"""
        s = self._dry_state(
            threshold=-1,
            rounds=[self._round() for _ in range(10)],
            known={"mod_a": ["A1"]},
        )
        self.assertEqual(auto_disable._decide(s), [])

    def test_threshold_one_disables_immediately(self):
        """threshold=1 时，只要最近一轮未用即触发禁用（dry_run 模式）。"""
        s = self._dry_state(
            threshold=1,
            rounds=[self._round()],
            known={"mod_a": ["A1"], "mod_b": ["B1", "B2"]},
        )
        newly = auto_disable._decide(s)
        self.assertCountEqual(newly, ["mod_a", "mod_b"])
        # dry_run 应当写入 disabled 条目并标注 status
        self.assertEqual(s["disabled"]["mod_a"]["status"], "dry_run")
        self.assertEqual(s["disabled"]["mod_b"]["status"], "dry_run")

    def test_threshold_three_two_unused_rounds_still_trigger(self):
        """threshold=3 时，2 轮全部无 usage 仍会触发禁用（产品当前行为）。

        注：产品代码采用 ``recent = rounds[-threshold:] if len(rounds) >= threshold
        else rounds`` 的语义；当 rounds < threshold 时仍以全部 rounds 作为视窗，
        因此连续无 usage 即触发，不等满 threshold 轮。"""
        s = self._dry_state(
            threshold=3,
            rounds=[self._round() for _ in range(2)],
            known={"mod_a": ["A1"]},
        )
        self.assertEqual(auto_disable._decide(s), ["mod_a"])

    def test_threshold_three_disables_after_three_unused_rounds(self):
        """threshold=3 时，连续 3 轮未用即触发禁用。"""
        s = self._dry_state(
            threshold=3,
            rounds=[self._round() for _ in range(3)],
            known={"mod_a": ["A1"]},
        )
        self.assertEqual(auto_disable._decide(s), ["mod_a"])

    def test_excluded_module_is_never_disabled(self):
        """``exclude`` 中的模块应被跳过；不在的仍可被禁用。"""
        s = self._dry_state(
            threshold=1,
            rounds=[self._round()],
            exclude=["mod_a"],
            known={"mod_a": ["A1"], "mod_b": ["B1"]},
        )
        newly = auto_disable._decide(s)
        self.assertNotIn("mod_a", newly)
        self.assertIn("mod_b", newly)

    def test_empty_known_modules_disables_nothing(self):
        """known_modules 为空时不应触发任何禁用。"""
        s = self._dry_state(threshold=1, rounds=[self._round()])
        self.assertEqual(auto_disable._decide(s), [])

    def test_already_disabled_module_is_skipped(self):
        """已经在 ``disabled`` 里的模块不应再次进入决策（去重）。"""
        s = self._dry_state(
            threshold=1,
            rounds=[self._round()],
            known={"mod_a": ["A1"]},
        )
        s["disabled"]["mod_a"] = {
            "original_path": "",
            "disabled_at": 0.0,
            "status": "confirmed",
        }
        self.assertEqual(auto_disable._decide(s), [])


# ---------------------------------------------------------------------------
# 2. 轮次不足
# ---------------------------------------------------------------------------


class InsufficientRoundsTests(_IsolatedTestBase):
    """rounds 数量 < threshold 时不触发决策；超出时只看最近 N 轮。"""

    def test_no_rounds_at_all_disables_nothing(self):
        s = self._dry_state(threshold=3, rounds=[], known={"mod_a": ["A1"]})
        self.assertEqual(auto_disable._decide(s), [])

    def test_two_rounds_below_threshold_with_no_usage_still_triggers(self):
        """rounds=2 < threshold=3 但无 usage，仍会触发禁用（产品当前行为）。

        见 ``test_threshold_three_two_unused_rounds_still_trigger`` 的说明：
        ``recent`` 在 rounds 不足时取全部 rounds，``used_union`` 为空集即触发。"""
        s = self._dry_state(
            threshold=3,
            rounds=[self._round()],
            known={"mod_a": ["A1"]},
        )
        self.assertEqual(auto_disable._decide(s), ["mod_a"])

    def test_rounds_equal_to_threshold_with_no_usage_disables(self):
        """rounds 数量刚好等于 threshold 且无使用时触发禁用。"""
        s = self._dry_state(
            threshold=3,
            rounds=[self._round() for _ in range(3)],
            known={"mod_a": ["A1"]},
        )
        self.assertEqual(auto_disable._decide(s), ["mod_a"])

    def test_single_used_round_below_threshold_does_not_disable(self):
        """rounds < threshold 但只要 usage 中出现过节点类，就不触发禁用。

        用于验证 ``isdisjoint(used_union)`` 的判定而非 round 数量本身。"""
        s = self._dry_state(
            threshold=3,
            rounds=[self._round("A1")],
            known={"mod_a": ["A1"]},
        )
        self.assertEqual(auto_disable._decide(s), [])

    def test_recent_window_only_uses_last_n_rounds(self):
        """rounds 数量大于 threshold 时，只看最近 N 轮。"""
        # 前 3 轮 mod_a 被使用，最后 2 轮没使用；
        # threshold=3 → 视窗只看最后 3 轮（含一轮使用过）→ 不应禁用。
        rounds = [
            self._round("A1", ts=1.0),
            self._round("A1", ts=2.0),
            self._round("A1", ts=3.0),
            self._round(ts=4.0),
            self._round(ts=5.0),
        ]
        s = self._dry_state(threshold=3, rounds=rounds, known={"mod_a": ["A1"]})
        self.assertEqual(auto_disable._decide(s), [])

    def test_partially_used_module_not_disabled(self):
        """只要某节点类出现在最近窗口内，整个模块都不应被禁用。"""
        rounds = [
            self._round("A1"),
            self._round(),
            self._round(),
        ]
        s = self._dry_state(
            threshold=3, rounds=rounds, known={"mod_a": ["A1", "A2"]}
        )
        self.assertEqual(auto_disable._decide(s), [])


# ---------------------------------------------------------------------------
# 3. 恢复回退（异常路径 + disable/restore roundtrip）
# ---------------------------------------------------------------------------


class RecoveryFallbackTests(_IsolatedTestBase):
    """``_disable_module`` / ``restore_module`` 在异常路径下的回退行为。"""

    def test_disable_module_with_missing_path_returns_false(self):
        """``module_path`` 指向不存在的路径时，禁用应失败。"""
        info = {
            "node_classes": ["A1"],
            "module_path": os.path.join(self.tmpdir, "no_such_dir"),
        }
        self.assertFalse(auto_disable._disable_module("mod_a", info))

    def test_disable_module_with_empty_path_returns_false(self):
        """``module_path`` 为空字符串时也应直接返回 False。"""
        info = {"node_classes": ["A1"], "module_path": ""}
        self.assertFalse(auto_disable._disable_module("mod_a", info))

    def test_restore_unknown_module_returns_false(self):
        """未在 ``disabled`` 记录里、``disabled_dir`` 下也不存在时返回 False。"""
        auto_disable._save_state(auto_disable._default_state())
        self.assertFalse(auto_disable.restore_module("ghost_module"))

    def test_disable_then_restore_roundtrip_succeeds(self):
        """正常的 disable → restore 往返，模块应当回到 custom_nodes/ 下。"""
        # 1) 在 custom_nodes 下创建一个真实模块目录
        mod_path = self._make_module("mod_a")

        # 2) 触发 _decide（dry_run=False，走真实移动路径）
        s = self._state(
            threshold=1,
            rounds=[self._round()],
            known={"mod_a": ["A1"]},
        )
        s["known_modules"]["mod_a"]["module_path"] = mod_path
        s["dry_run"] = False
        auto_disable._save_state(s)
        auto_disable._decide(s)
        auto_disable._save_state(s)

        # 模块应已被移到 .disabled/
        moved = os.path.join(self.disabled_dir, "mod_a")
        self.assertTrue(os.path.isdir(moved))
        self.assertFalse(os.path.exists(mod_path))
        # state 记录里也应出现且状态为 confirmed
        self.assertEqual(s["disabled"]["mod_a"]["status"], "confirmed")

        # 3) restore 应能把它放回原位
        self.assertTrue(auto_disable.restore_module("mod_a"))
        self.assertTrue(os.path.isdir(mod_path))
        self.assertFalse(os.path.exists(moved))

        # 4) state.disabled 应被清理
        s2 = auto_disable._load_state()
        self.assertNotIn("mod_a", s2.get("disabled", {}))

    def test_restore_aborts_when_destination_exists(self):
        """目标路径已存在时，restore 应中止并保留 disabled 记录。"""
        os.makedirs(os.path.join(self.disabled_dir, "mod_a"), exist_ok=True)
        os.makedirs(os.path.join(self.custom_nodes_dir, "mod_a"), exist_ok=True)
        s = auto_disable._default_state()
        s["disabled"]["mod_a"] = {
            "original_path": os.path.join(self.custom_nodes_dir, "mod_a"),
            "disabled_at": 0.0,
            "status": "confirmed",
        }
        auto_disable._save_state(s)

        self.assertFalse(auto_disable.restore_module("mod_a"))

        # state 记录仍存在
        s2 = auto_disable._load_state()
        self.assertIn("mod_a", s2.get("disabled", {}))

    def test_reconcile_pending_rolls_back_when_path_still_exists(self):
        """启动时若 ``status=pending`` 但原路径仍在，应回滚 disabled 记录。"""
        mod_path = self._make_module("mod_a")
        s = auto_disable._default_state()
        s["disabled"]["mod_a"] = {
            "original_path": mod_path,
            "disabled_at": 0.0,
            "status": "pending",
        }
        # 路径仍在 → 应回滚
        changed = auto_disable._reconcile_pending(s)
        self.assertTrue(changed)
        self.assertNotIn("mod_a", s["disabled"])

    def test_reconcile_pending_confirms_when_path_missing(self):
        """启动时若 ``status=pending`` 且路径已不在，应确认为 confirmed。"""
        s = auto_disable._default_state()
        s["disabled"]["mod_a"] = {
            "original_path": os.path.join(self.custom_nodes_dir, "mod_a"),
            "disabled_at": 0.0,
            "status": "pending",
        }
        changed = auto_disable._reconcile_pending(s)
        self.assertTrue(changed)
        self.assertEqual(s["disabled"]["mod_a"]["status"], "confirmed")


# ---------------------------------------------------------------------------
# 4b. 启动对账：.disabled/ 与 state['disabled'] 同步
# ---------------------------------------------------------------------------


class StartupReconcileDiskTests(_IsolatedTestBase):
    """启动时 ``_reconcile_disabled_with_disk`` 的四种场景。

    覆盖：
    - 状态与磁盘完全一致 → 不修改任何东西
    - 磁盘有、状态无 → 补齐状态（added）
    - 状态有、磁盘无、original_path 重新可见 → 删除状态记录（restored）
    - 状态有、磁盘无、original_path 也无 → 警告并保留（warnings）
    - ``_load_state`` 集成测试：状态文件落盘后下次启动能补齐
    """

    # ----- 场景 1：状态与磁盘一致 -----

    def test_no_change_when_state_and_disk_agree(self):
        """state['disabled'] 与 .disabled/ 一一对应时，不做任何变更。"""
        # 构造一个真实禁用模块（同时落入 state 与磁盘）
        mod_path = self._make_module("mod_a")
        s = auto_disable._default_state()
        s["disabled"]["mod_a"] = {
            "original_path": mod_path,
            "disabled_at": 0.0,
            "status": "confirmed",
            "node_classes": ["ClassA"],
        }
        # 手动把磁盘上的 mod_a 移到 .disabled/，模拟"已禁用"状态
        os.makedirs(self.disabled_dir, exist_ok=True)
        shutil.move(mod_path, os.path.join(self.disabled_dir, "mod_a"))
        auto_disable._save_state(s)

        result = auto_disable._reconcile_disabled_with_disk(s)

        self.assertFalse(result["changed"])
        self.assertEqual(result["added"], [])
        self.assertEqual(result["restored"], [])
        self.assertEqual(result["warnings"], [])
        self.assertIn("mod_a", s["disabled"])

    # ----- 场景 2：磁盘有、状态无 → 补齐 -----

    def test_disk_only_entry_is_added_to_state(self):
        """磁盘上 .disabled/ 有但 state 里没有时，应追加到 state。"""
        os.makedirs(self.disabled_dir, exist_ok=True)
        # 手动在磁盘上建一个禁用模块
        os.makedirs(os.path.join(self.disabled_dir, "external_mod"), exist_ok=True)
        s = auto_disable._default_state()
        auto_disable._save_state(s)

        result = auto_disable._reconcile_disabled_with_disk(s)

        self.assertTrue(result["changed"])
        self.assertEqual(result["added"], ["external_mod"])
        self.assertEqual(result["restored"], [])
        # state 应被补齐
        self.assertIn("external_mod", s["disabled"])
        info = s["disabled"]["external_mod"]
        self.assertEqual(info["status"], "confirmed")
        self.assertEqual(info["original_path"], "")
        self.assertEqual(info["node_classes"], [])

    def test_disk_only_file_is_added_to_state(self):
        """磁盘上是单文件（不是目录）时也应被识别并补齐。"""
        os.makedirs(self.disabled_dir, exist_ok=True)
        # 单文件模块也是合法形态（_disable_module 可以移文件）
        with open(
            os.path.join(self.disabled_dir, "single.py"), "w", encoding="utf-8"
        ) as f:
            f.write("# disabled\n")
        s = auto_disable._default_state()
        auto_disable._save_state(s)

        result = auto_disable._reconcile_disabled_with_disk(s)

        self.assertEqual(result["added"], ["single.py"])
        self.assertIn("single.py", s["disabled"])

    # ----- 场景 3：状态有、磁盘无、original_path 可见 → 删除 -----

    def test_state_entry_with_restored_original_path_is_removed(self):
        """state 里有记录但磁盘上 .disabled/ 已无，且 original_path 重新可见
        （说明用户手动恢复过）→ 从 state 中清理。"""
        # original_path 是真实存在的目录，模拟"已被用户手工移回"
        mod_path = self._make_module("manual_restore")
        s = auto_disable._default_state()
        s["disabled"]["manual_restore"] = {
            "original_path": mod_path,
            "disabled_at": 1.0,
            "status": "confirmed",
            "node_classes": ["ClassZ"],
        }
        auto_disable._save_state(s)
        # .disabled/ 里没有 manual_restore（磁盘上为空）

        result = auto_disable._reconcile_disabled_with_disk(s)

        self.assertTrue(result["changed"])
        self.assertEqual(result["restored"], ["manual_restore"])
        self.assertEqual(result["added"], [])
        self.assertNotIn("manual_restore", s["disabled"])

    # ----- 场景 4：状态有、磁盘无、original_path 也无 → 警告保留 -----

    def test_orphan_state_entry_is_warned_but_kept(self):
        """state 里有记录但磁盘与 original_path 都不在 → 警告并保留。"""
        s = auto_disable._default_state()
        s["disabled"]["orphan_mod"] = {
            "original_path": os.path.join(self.tmpdir, "definitely_missing"),
            "disabled_at": 1.0,
            "status": "confirmed",
            "node_classes": ["ClassY"],
        }
        auto_disable._save_state(s)

        result = auto_disable._reconcile_disabled_with_disk(s)

        # changed=False（我们没有删除它），但加入 warnings
        self.assertFalse(result["changed"])
        self.assertEqual(result["warnings"], ["orphan_mod"])
        # 记录仍存在
        self.assertIn("orphan_mod", s["disabled"])

    def test_orphan_with_empty_original_path_is_warned_but_kept(self):
        """original_path 为空字符串的孤儿条目也应保留。"""
        s = auto_disable._default_state()
        s["disabled"]["orphan_empty"] = {
            "original_path": "",
            "disabled_at": 1.0,
            "status": "confirmed",
            "node_classes": [],
        }
        auto_disable._save_state(s)

        result = auto_disable._reconcile_disabled_with_disk(s)

        self.assertFalse(result["changed"])
        self.assertEqual(result["warnings"], ["orphan_empty"])
        self.assertIn("orphan_empty", s["disabled"])

    # ----- 场景 5：混合场景 -----

    def test_mixed_scenario_handles_each_case_independently(self):
        """state 与磁盘混合不一致时，4 类场景同时触发，各自处理。"""
        os.makedirs(self.disabled_dir, exist_ok=True)
        # 磁盘上：disk_only（场景 2）+ on_disk_normal（场景 1）
        os.makedirs(os.path.join(self.disabled_dir, "disk_only"), exist_ok=True)
        on_disk_path = os.path.join(self.disabled_dir, "on_disk_normal")
        os.makedirs(on_disk_path, exist_ok=True)
        # 自建一个被用户手动移回的模块
        manual_path = self._make_module("manual_restored")

        s = auto_disable._default_state()
        s["disabled"]["on_disk_normal"] = {
            "original_path": "",
            "disabled_at": 0.0,
            "status": "confirmed",
        }
        s["disabled"]["manual_restored"] = {
            "original_path": manual_path,
            "disabled_at": 0.0,
            "status": "confirmed",
        }
        s["disabled"]["orphan"] = {
            "original_path": os.path.join(self.tmpdir, "no_such_path"),
            "disabled_at": 0.0,
            "status": "confirmed",
        }
        auto_disable._save_state(s)

        result = auto_disable._reconcile_disabled_with_disk(s)

        self.assertTrue(result["changed"])
        self.assertEqual(result["added"], ["disk_only"])
        self.assertEqual(result["restored"], ["manual_restored"])
        self.assertEqual(result["warnings"], ["orphan"])
        # on_disk_normal 保持不变
        self.assertIn("on_disk_normal", s["disabled"])
        # manual_restored 应被清掉
        self.assertNotIn("manual_restored", s["disabled"])
        # orphan 应保留
        self.assertIn("orphan", s["disabled"])
        # disk_only 应被补齐
        self.assertIn("disk_only", s["disabled"])

    # ----- 集成：通过 _load_state 触发对账 -----

    def test_load_state_persists_disk_only_additions(self):
        """``_load_state`` 加载时发现磁盘独有条目，落盘后状态文件更新。"""
        os.makedirs(self.disabled_dir, exist_ok=True)
        os.makedirs(os.path.join(self.disabled_dir, "outside_mod"), exist_ok=True)

        # 初始 state 文件（不包含 outside_mod）
        s0 = auto_disable._default_state()
        auto_disable._save_state(s0)

        # 重新加载（触发对账 + 持久化）
        auto_disable._load_state()

        # 再读一次确认已落盘
        s_after = auto_disable._load_state()
        self.assertIn("outside_mod", s_after["disabled"])
        self.assertEqual(s_after["disabled"]["outside_mod"]["status"], "confirmed")


# ---------------------------------------------------------------------------
# 4. 窗口修剪
# ---------------------------------------------------------------------------


class WindowPruningTests(_IsolatedTestBase):
    """``record_prompt`` 中 ``rounds`` 超 ``keep * 4`` 时被截断。"""

    def test_window_pruning_truncates_when_over_keep_quadruple(self):
        threshold = 3
        keep = self._keep(threshold)  # = 5
        n = keep * 4 + 10  # 30 条，超出 keep*4=20
        s = self._state(threshold=threshold, rounds=[
            self._round(ts=float(i)) for i in range(n)
        ])
        auto_disable._save_state(s)

        auto_disable.record_prompt([])  # 追加 1 条后再触发修剪

        new_state = auto_disable._load_state()
        # record_prompt 先 append，再裁剪到 keep*4 → 应当剩 20 条
        self.assertEqual(len(new_state["rounds"]), keep * 4)

    def test_window_pruning_preserves_most_recent(self):
        """裁剪后保留的应是最近 ``keep*4`` 条（含新增的那一条）。"""
        threshold = 3
        keep = self._keep(threshold)
        n = keep * 4 + 5
        s = self._state(threshold=threshold, rounds=[
            self._round("OLD", ts=float(i)) for i in range(n)
        ])
        auto_disable._save_state(s)

        auto_disable.record_prompt(["NEW_TAG"])

        new_state = auto_disable._load_state()
        rounds = new_state["rounds"]
        self.assertEqual(len(rounds), keep * 4)
        # 最近一轮应是这次 record_prompt 的内容
        self.assertEqual(rounds[-1]["used_classes"], ["NEW_TAG"])
        # 倒数第二轮应是原始第 n-1 条
        self.assertEqual(rounds[-2]["used_classes"], ["OLD"])

    def test_window_no_pruning_when_under_limit(self):
        """``rounds`` 数量严格小于 ``keep*4`` 时不应修剪（负面验证）。"""
        threshold = 3
        keep = self._keep(threshold)  # = 5
        # 关键边界：刚好 keep*4 - 1 条 → append 后为 keep*4 → 不再 > 上限 → 不修剪
        n = keep * 4 - 1  # 19
        s = self._state(threshold=threshold, rounds=[
            self._round(ts=float(i)) for i in range(n)
        ])
        auto_disable._save_state(s)

        auto_disable.record_prompt([])

        new_state = auto_disable._load_state()
        # append 后应为 keep*4 条，未触发修剪
        self.assertEqual(len(new_state["rounds"]), keep * 4)

    def test_record_prompt_writes_prompt_id_audit_field(self):
        """``record_prompt`` 应把 ``prompt_id`` 透传到最新一条 round。"""
        s = self._state(threshold=3, rounds=[])
        auto_disable._save_state(s)

        auto_disable.record_prompt(["A1"], prompt_id="pid-123")

        new_state = auto_disable._load_state()
        self.assertEqual(new_state["rounds"][-1].get("prompt_id"), "pid-123")

    def test_record_prompt_returns_progress_summary(self):
        """``record_prompt`` 返回进度摘要，供调用方记录到日志。"""
        s = self._state(threshold=7, rounds=[self._round() for _ in range(4)])
        s["dry_run"] = False
        s["known_modules"] = self._known({"mod_a": ["A1"], "mod_b": ["B1"]})
        auto_disable._save_state(s)

        summary = auto_disable.record_prompt(["A1", "B2"])

        self.assertIsInstance(summary, dict)
        self.assertEqual(summary["threshold"], 7)
        # keep = max(threshold + 2, 5) = max(9, 5) = 9
        self.assertEqual(summary["keep"], 9)
        # cap = keep * 4
        self.assertEqual(summary["cap"], 36)
        # 原本 4 轮，append 后 5 轮
        self.assertEqual(summary["rounds_count"], 5)
        self.assertEqual(summary["dry_run"], False)
        self.assertEqual(summary["known_count"], 2)
        self.assertEqual(summary["disabled_count"], 0)
        self.assertEqual(summary["newly_disabled"], [])

    def test_record_prompt_summary_reflects_dry_run(self):
        """``dry_run=True`` 状态返回的摘要应带 ``dry_run=True``。"""
        s = self._state(threshold=3, rounds=[])
        s["dry_run"] = True
        s["known_modules"] = self._known({"mod_a": ["A1"]})
        auto_disable._save_state(s)

        summary = auto_disable.record_prompt(["A1"])

        self.assertEqual(summary["dry_run"], True)
        self.assertEqual(summary["threshold"], 3)
        self.assertEqual(summary["rounds_count"], 1)
        # dry_run=True 时，A1 与 mod_a 重叠，不应被加入 disabled
        self.assertEqual(summary["known_count"], 1)
        self.assertEqual(summary["disabled_count"], 0)

    def test_record_prompt_summary_records_newly_disabled(self):
        """当本次入队导致一个已知模块被禁用时，摘要应反映出来。"""
        # 在临时目录里真的建一个被禁用的模块，_disable_module 才能成功移动。
        mod_path = self._make_module("ghost_mod")
        s = self._state(threshold=1, rounds=[])
        # known_modules 必须带 module_path，否则 _disable_module 拿不到源路径会回滚。
        s["known_modules"] = {
            "ghost_mod": {"node_classes": ["GhostClass"], "module_path": mod_path},
        }
        auto_disable._save_state(s)

        summary = auto_disable.record_prompt(["A1", "B2"])

        self.assertEqual(summary["newly_disabled"], ["ghost_mod"])
        self.assertEqual(summary["disabled_count"], 1)
        self.assertEqual(summary["known_count"], 1)
        self.assertEqual(summary["threshold"], 1)

    def test_disabled_exceeds_known_when_stale_entries_exist(self):
        """``disabled`` 比 ``known`` 多时（陈旧条目），摘要如实反映两者。"""
        # 建一个真正被禁用的模块（custom_nodes/ → .disabled/ 物理移动）
        mod_path = self._make_module("kept_mod")
        os.makedirs(self.disabled_dir, exist_ok=True)
        shutil.move(mod_path, os.path.join(self.disabled_dir, "kept_mod"))
        s = self._state(threshold=3, rounds=[])
        # known 1 个，但 disabled 字典里已经预先塞了 3 条陈旧记录
        s["known_modules"] = {
            "kept_mod": {"node_classes": ["KeptClass"], "module_path": mod_path},
        }
        s["disabled"] = {
            # 2 条孤儿：不在磁盘上、original_path 也为空 → 对账后保留为 warning
            "old_mod_a": {
                "original_path": "", "disabled_at": 0.0,
                "status": "confirmed",
            },
            "old_mod_b": {
                "original_path": "", "disabled_at": 0.0,
                "status": "confirmed",
            },
            # 1 条正常：on_disk 验证过 original_path 存在、被 reconcile 保留
            "kept_mod": {
                "original_path": mod_path, "disabled_at": 0.0,
                "status": "confirmed",
            },
        }
        auto_disable._save_state(s)

        summary = auto_disable.record_prompt(["X"])

        self.assertEqual(summary["known_count"], 1)
        self.assertEqual(summary["disabled_count"], 3)
        # 3 > 1，应当是“全部 known 被禁 + 2 条陈旧”
        self.assertGreater(summary["disabled_count"], summary["known_count"])
        # 本轮【没有新增】禁用（reconcile 已把 kept_mod 视为已确认禁用）
        self.assertEqual(summary["newly_disabled"], [])


# ---------------------------------------------------------------------------
# 6. 刻意构造的失败用例（用于展示测试网格的捕获能力）
# ---------------------------------------------------------------------------


class DeliberateFailureDetectionTests(_IsolatedTestBase):
    """通过反向断言验证：若产品行为回归，本网格的测试能立即捕获。"""

    def test_threshold_zero_short_circuit(self):
        """若 ``_decide`` 在 threshold=0 时错误地尝试扫描，应被本测试捕获。"""
        s = self._dry_state(
            threshold=0,
            rounds=[self._round() for _ in range(5)],
            known={"mod_a": ["A1"]},
        )
        self.assertEqual(
            auto_disable._decide(s),
            [],
            "threshold=0 时不应触发禁用；若失败说明阈值判定出现回归。",
        )

    def test_restore_does_not_delete_disabled_record_on_failure(self):
        """restore 失败时不应清理 ``state.disabled``（保护用户记录）。"""
        os.makedirs(os.path.join(self.disabled_dir, "mod_a"), exist_ok=True)
        os.makedirs(os.path.join(self.custom_nodes_dir, "mod_a"), exist_ok=True)
        s = auto_disable._default_state()
        s["disabled"]["mod_a"] = {
            "original_path": os.path.join(self.custom_nodes_dir, "mod_a"),
            "disabled_at": 1.0,
            "status": "confirmed",
        }
        auto_disable._save_state(s)

        self.assertFalse(auto_disable.restore_module("mod_a"))

        s2 = auto_disable._load_state()
        # 关键负面断言：失败时 disabled 记录必须仍在
        self.assertIn(
            "mod_a",
            s2.get("disabled", {}),
            "restore 失败不应清理 disabled 记录；若失败说明恢复路径出现回归。",
        )

    def test_window_pruning_lower_bound(self):
        """验证 ``> keep*4`` 这一修剪条件：刚好 keep*4 - 1 时不应修剪。"""
        threshold = 3
        keep = self._keep(threshold)  # = 5
        # 关键边界：keep*4 - 1 条 → append 后 keep*4 条 → 等于上界 → 不修剪
        n = keep * 4 - 1
        s = self._state(threshold=threshold, rounds=[
            self._round(ts=float(i)) for i in range(n)
        ])
        auto_disable._save_state(s)

        auto_disable.record_prompt([])

        rounds = auto_disable._load_state()["rounds"]
        self.assertEqual(
            len(rounds),
            keep * 4,
            "刚好 keep*4 - 1 条时 append 后应为 keep*4，不触发修剪。",
        )

    def test_dry_run_records_status_without_moving_files(self):
        """dry_run=True 时 ``_decide`` 应只写审计字段，不应触发目录移动。"""
        mod_path = self._make_module("mod_a")
        s = self._dry_state(
            threshold=1,
            rounds=[self._round()],
            known={"mod_a": ["A1"]},
        )
        s["known_modules"]["mod_a"]["module_path"] = mod_path

        newly = auto_disable._decide(s)

        # 决策结果应包含该模块
        self.assertEqual(newly, ["mod_a"])
        # 文件系统不应发生变化
        self.assertTrue(os.path.isdir(mod_path))
        self.assertFalse(os.path.exists(os.path.join(self.disabled_dir, "mod_a")))
        # 但 disabled 条目里应有 dry_run 状态
        self.assertEqual(s["disabled"]["mod_a"]["status"], "dry_run")


# ---------------------------------------------------------------------------
# 7. 缺失节点自动恢复：submit 引用未注册类 → .disabled 匹配 → 原子恢复
# ---------------------------------------------------------------------------


class MissingNodeAutoRestoreTests(_IsolatedTestBase):
    """submit 引用了当前未注册节点类时，从 .disabled/ 自动匹配并恢复。

    覆盖以下场景：
    - 缺失类能匹配到 .disabled 中某个 confirmed 模块 → 物理恢复 + 清理记录
    - .disabled 中无匹配 → 不动状态、不写 pending_restart
    - pending/dry_run 状态的 disabled 记录不参与自动恢复（无物理目录可移）
    - 多个缺失类对应多个模块 → 一次性恢复 + 一次性入 pending_restart
    - 目标位置已被占用时，恢复失败但 disabled 记录保留
    - record_prompt 入口会复用 _scan_known_modules 的副作用（mock 跳过即可）
    - pending_restart 写入磁盘后能被 consume_pending_restart 消费并清空
    - nodes 模块导入失败时（=拿不到 registered），跳过整次恢复
    """

    # ---------- 辅助 ----------

    def _disable_one(self, name, node_classes):
        """在 ``custom_nodes/<name>/`` 造一个真实目录、再走 ``_decide`` 把
        它物理移到 ``.disabled/<name>/``，并把 node_classes 写进 disabled 记录。
        返回 ``original_path``。

        会保留之前调用留下的 ``state["disabled"]`` 其它条目，避免连续调用
        把后续模块的 disabled 记录被空 state 覆盖。
        """
        mod_path = self._make_module(name)
        # 从现有文件加载（保留之前 _disable_one 留下的 disabled 记录）
        try:
            s = auto_disable._load_state()
        except Exception:
            s = auto_disable._default_state()
        s["threshold"] = 1
        s["rounds"] = [self._round()]
        s.setdefault("exclude", [])
        s.setdefault("known_modules", {})
        s["known_modules"][name] = {
            "node_classes": list(node_classes),
            "module_path": mod_path,
        }
        s["dry_run"] = False
        auto_disable._save_state(s)
        auto_disable._decide(s)
        auto_disable._save_state(s)
        # 确认已移动且记录为 confirmed
        self.assertEqual(s["disabled"][name]["status"], "confirmed")
        self.assertEqual(
            sorted(s["disabled"][name]["node_classes"]),
            sorted(node_classes),
        )
        self.assertFalse(os.path.exists(mod_path))
        return mod_path

    # ---------- 正常路径 ----------

    def test_record_prompt_restores_disabled_module_for_missing_class(self):
        """缺失节点类被命中 → 自动恢复到 custom_nodes/。"""
        # 1) 先把 mod_a 禁用掉（提供 ClassA）
        self._disable_one("mod_a", ["ClassA"])
        # 2) 模拟当前进程只注册了 ClassB，没有 ClassA
        with mock.patch.object(
            auto_disable, "_current_registered_classes",
            return_value={"ClassB"},
        ):
            # 从文件加载 state（_disable_one 已经把记录落盘）
            s = auto_disable._load_state()
            restored = auto_disable.restore_for_missing_classes(
                s, ["ClassA", "ClassB"], prompt_id="pid-restore-1",
            )
        # 3) mod_a 应被恢复、disabled 记录被清理
        self.assertEqual(restored, ["mod_a"])
        self.assertNotIn("mod_a", s["disabled"])
        self.assertTrue(os.path.isdir(os.path.join(self.custom_nodes_dir, "mod_a")))
        self.assertFalse(os.path.exists(os.path.join(self.disabled_dir, "mod_a")))
        # 4) pending_restart 应当包含这次恢复
        self.assertEqual(len(s["pending_restart"]), 1)
        item = s["pending_restart"][0]
        self.assertEqual(item["module"], "mod_a")
        self.assertEqual(item["prompt_id"], "pid-restore-1")
        self.assertEqual(sorted(item["node_classes"]), ["ClassA"])

    def test_no_match_in_disabled_leaves_state_intact(self):
        """缺失类在 .disabled 中找不到匹配时，不做任何状态变更。"""
        self._disable_one("mod_a", ["ClassA"])
        with mock.patch.object(
            auto_disable, "_current_registered_classes",
            return_value={"ClassB"},
        ):
            s = auto_disable._load_state()
            restored = auto_disable.restore_for_missing_classes(
                s, ["ClassX"],  # 不属于 mod_a
            )
        self.assertEqual(restored, [])
        self.assertIn("mod_a", s["disabled"])
        self.assertEqual(s["pending_restart"], [])
        # mod_a 物理位置不变
        self.assertTrue(os.path.isdir(os.path.join(self.disabled_dir, "mod_a")))

    def test_pending_and_dry_run_disabled_are_not_restored(self):
        """status=pending / dry_run 不参与自动恢复（不在磁盘上）。"""
        s = auto_disable._default_state()
        s["disabled"]["mod_a"] = {
            "original_path": os.path.join(self.custom_nodes_dir, "mod_a"),
            "disabled_at": 0.0,
            "node_classes": ["ClassA"],
            "status": "pending",
        }
        s["disabled"]["mod_b"] = {
            "original_path": os.path.join(self.custom_nodes_dir, "mod_b"),
            "disabled_at": 0.0,
            "node_classes": ["ClassB"],
            "status": "dry_run",
        }
        with mock.patch.object(
            auto_disable, "_current_registered_classes",
            return_value=set(),
        ):
            restored = auto_disable.restore_for_missing_classes(
                s, ["ClassA", "ClassB"],
            )
        self.assertEqual(restored, [])
        # 两条记录都原样保留
        self.assertIn("mod_a", s["disabled"])
        self.assertIn("mod_b", s["disabled"])

    def test_multiple_missing_classes_restore_multiple_modules(self):
        """多个缺失类能匹配到多个 disabled 模块时，一次性全部恢复。"""
        self._disable_one("mod_a", ["ClassA"])
        self._disable_one("mod_b", ["ClassB1", "ClassB2"])
        with mock.patch.object(
            auto_disable, "_current_registered_classes",
            return_value=set(),
        ):
            s = auto_disable._load_state()
            restored = auto_disable.restore_for_missing_classes(
                s, ["ClassA", "ClassB1"],
            )
        self.assertCountEqual(restored, ["mod_a", "mod_b"])
        self.assertNotIn("mod_a", s["disabled"])
        self.assertNotIn("mod_b", s["disabled"])
        self.assertEqual(len(s["pending_restart"]), 2)
        names = {it["module"] for it in s["pending_restart"]}
        self.assertEqual(names, {"mod_a", "mod_b"})

    def test_disabled_entry_without_node_classes_is_skipped(self):
        """旧版本写下的 disabled 记录若没有 node_classes 字段，应跳过以免误伤。"""
        os.makedirs(os.path.join(self.disabled_dir, "mod_a"), exist_ok=True)
        s = auto_disable._default_state()
        s["disabled"]["mod_a"] = {
            "original_path": os.path.join(self.custom_nodes_dir, "mod_a"),
            "disabled_at": 0.0,
            "status": "confirmed",
            # 故意缺 node_classes
        }
        with mock.patch.object(
            auto_disable, "_current_registered_classes",
            return_value=set(),
        ):
            restored = auto_disable.restore_for_missing_classes(
                s, ["ClassAnything"],
            )
        self.assertEqual(restored, [])
        # 原 .disabled/ 目录应原样保留
        self.assertTrue(os.path.isdir(os.path.join(self.disabled_dir, "mod_a")))

    def test_destination_already_exists_blocks_restore_and_keeps_record(self):
        """目标位置已有同名模块时，恢复失败，disabled 记录必须保留。"""
        # 先禁掉 mod_a，同时在 custom_nodes 里留一个同名空目录（模拟冲突）
        self._disable_one("mod_a", ["ClassA"])
        blocker = os.path.join(self.custom_nodes_dir, "mod_a")
        os.makedirs(blocker, exist_ok=True)
        with mock.patch.object(
            auto_disable, "_current_registered_classes",
            return_value=set(),
        ):
            s = auto_disable._load_state()
            restored = auto_disable.restore_for_missing_classes(
                s, ["ClassA"],
            )
        self.assertEqual(restored, [])
        # disabled 记录仍在，pending_restart 不应有该模块的条目
        self.assertIn("mod_a", s["disabled"])
        self.assertEqual(s["pending_restart"], [])

    def test_nodes_unavailable_skips_restore_safely(self):
        """_current_registered_classes 返回 None（未知状态）时应跳过恢复。"""
        self._disable_one("mod_a", ["ClassA"])
        with mock.patch.object(
            auto_disable, "_current_registered_classes",
            return_value=None,  # 未知：nodes 模块导入失败
        ):
            s = auto_disable._load_state()
            restored = auto_disable.restore_for_missing_classes(
                s, ["ClassA"],
            )
        self.assertEqual(restored, [])
        self.assertIn("mod_a", s["disabled"])
        self.assertTrue(os.path.isdir(os.path.join(self.disabled_dir, "mod_a")))

    def test_empty_registered_set_treats_all_used_as_missing(self):
        """_current_registered_classes 返回空 set（已知为空）时，任何 used 都视为缺失。"""
        self._disable_one("mod_a", ["ClassA"])
        with mock.patch.object(
            auto_disable, "_current_registered_classes",
            return_value=set(),  # 已知为空：进程里没有任何节点类被注册
        ):
            s = auto_disable._load_state()
            restored = auto_disable.restore_for_missing_classes(
                s, ["ClassA"],
            )
        self.assertEqual(restored, ["mod_a"])
        self.assertNotIn("mod_a", s["disabled"])

    def test_empty_used_classes_is_noop(self):
        """used_classes 为空时不应触发任何动作。"""
        self._disable_one("mod_a", ["ClassA"])
        with mock.patch.object(
            auto_disable, "_current_registered_classes",
            return_value=set(),
        ):
            s = auto_disable._load_state()
            restored = auto_disable.restore_for_missing_classes(s, [])
        self.assertEqual(restored, [])
        self.assertEqual(s["pending_restart"], [])
        self.assertIn("mod_a", s["disabled"])

    def test_all_used_classes_already_registered_is_noop(self):
        """本次用到的类全部已注册时，不应触发任何动作。"""
        self._disable_one("mod_a", ["ClassA"])
        with mock.patch.object(
            auto_disable, "_current_registered_classes",
            return_value={"ClassA", "ClassOther"},
        ):
            s = auto_disable._load_state()
            restored = auto_disable.restore_for_missing_classes(
                s, ["ClassA", "ClassOther"],
            )
        self.assertEqual(restored, [])
        self.assertEqual(s["pending_restart"], [])
        self.assertIn("mod_a", s["disabled"])

    # ---------- 持久化与消费 ----------

    def test_consume_pending_restart_clears_and_returns_items(self):
        """consume_pending_restart 应返回当前条目并清空。"""
        # 准备：状态里有两条 pending_restart
        auto_disable._save_state({
            "threshold": 30,
            "dry_run": False,
            "exclude": [],
            "known_modules": {},
            "rounds": [],
            "disabled": {},
            "pending_restart": [
                {"module": "mod_a", "node_classes": ["ClassA"], "restored_at": 1.0, "prompt_id": "p1"},
                {"module": "mod_b", "node_classes": ["ClassB"], "restored_at": 2.0, "prompt_id": "p2"},
            ],
        })
        items = auto_disable.consume_pending_restart()
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["module"], "mod_a")
        self.assertEqual(items[1]["module"], "mod_b")
        # 再消费应为空
        self.assertEqual(auto_disable.consume_pending_restart(), [])
        # 状态文件里也应是空 list
        s2 = auto_disable._load_state()
        self.assertEqual(s2.get("pending_restart"), [])

    def test_pending_restart_persisted_to_disk_after_restore(self):
        """restore_for_missing_classes 命中后应把 pending_restart 写盘。"""
        self._disable_one("mod_a", ["ClassA"])
        with mock.patch.object(
            auto_disable, "_current_registered_classes",
            return_value=set(),
        ):
            s = auto_disable._load_state()
            auto_disable.restore_for_missing_classes(s, ["ClassA"], prompt_id="p-disk")
        # 直接读状态文件确认
        with open(self.state_file, "r", encoding="utf-8") as f:
            persisted = json.load(f)
        self.assertEqual(len(persisted["pending_restart"]), 1)
        self.assertEqual(persisted["pending_restart"][0]["module"], "mod_a")
        self.assertEqual(persisted["pending_restart"][0]["prompt_id"], "p-disk")
        # disabled 应已被清理
        self.assertNotIn("mod_a", persisted["disabled"])

    def test_record_prompt_triggers_restore_before_decide(self):
        """record_prompt 入口会在 _decide 之前自动跑 restore_for_missing_classes。"""
        # 预置一个 confirmed disabled 记录，并把对应模块物理移到 .disabled
        self._disable_one("mod_a", ["ClassA"])
        # 模拟当前节点注册集（不含 ClassA）
        with mock.patch.object(
            auto_disable, "_current_registered_classes",
            return_value=set(),
        ):
            auto_disable.record_prompt(["ClassA"], prompt_id="pid-rp")
        # 落盘后状态文件里：mod_a 应已恢复，pending_restart 有该条目
        with open(self.state_file, "r", encoding="utf-8") as f:
            persisted = json.load(f)
        self.assertNotIn("mod_a", persisted["disabled"])
        self.assertEqual(len(persisted["pending_restart"]), 1)
        self.assertEqual(persisted["pending_restart"][0]["module"], "mod_a")
        self.assertEqual(persisted["pending_restart"][0]["prompt_id"], "pid-rp")
        # 文件系统也回到了 custom_nodes
        self.assertTrue(os.path.isdir(os.path.join(self.custom_nodes_dir, "mod_a")))

    def test_record_prompt_with_no_missing_does_not_touch_disabled(self):
        """本次用到的类全部已注册时，record_prompt 不会动 disabled 也不写 pending。"""
        self._disable_one("mod_a", ["ClassA"])
        with mock.patch.object(
            auto_disable, "_current_registered_classes",
            return_value={"ClassA"},
        ):
            auto_disable.record_prompt(["ClassA"], prompt_id="pid-skip")
        with open(self.state_file, "r", encoding="utf-8") as f:
            persisted = json.load(f)
        self.assertIn("mod_a", persisted["disabled"])
        self.assertEqual(persisted.get("pending_restart", []), [])

    def test_pending_restart_defaults_when_missing_in_old_state_file(self):
        """旧版本状态文件缺 pending_restart 字段时，加载后应被补齐为 []。"""
        legacy = auto_disable._default_state()
        legacy.pop("pending_restart", None)
        auto_disable._atomic_write_json(self.state_file, legacy)
        loaded = auto_disable._load_state()
        self.assertEqual(loaded.get("pending_restart"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
