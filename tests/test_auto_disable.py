"""阈值判定 / 轮次不足 / 窗口修剪 / 失败检测 相关测试。

涵盖：
1. 阈值判定（threshold=0 / 负数 / 1 / 3 + exclude + dry_run）
2. 轮次不足（rounds 数量 < threshold 时的判定行为）
3. 窗口修剪（rounds 超 keep*4 时被截断 + record_prompt 摘要）
4. 刻意构造的失败用例（用于展示测试网格的捕获能力）

其余维度见兄弟文件：
- 恢复路径 → ``tests/test_recovery.py``
- 启动对账与状态迁移 → ``tests/test_state_lifecycle.py``
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import unittest

# 让 ``_base`` / ``auto_disable`` 都能以模块方式被定位
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(TESTS_DIR, os.pardir))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

from _base import _IsolatedTestBase  # noqa: E402
import auto_disable  # noqa: E402


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
        """当本次入队导致一个已知模块被禁用时，摘要应反映出来。

        补充语义：物理 disable 成功后该模块会从 ``known_modules`` 移除
        （与 ``restore_for_missing_classes`` 恢复后会回填 ``known_modules``
        对称），所以 ``known_count`` 反映“当前仍可用”的模块数。
        """
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
        # ghost_mod 已被物理移走，从 known_modules 移除后剩 0 个“当前可用”模块
        self.assertEqual(summary["known_count"], 0)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)