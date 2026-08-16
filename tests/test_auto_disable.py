"""auto_disable 最小测试网格。

覆盖 4 个关键判定维度：
  1. 阈值判定（threshold=0/-1/1/3 + exclude）
  2. 轮次不足（rounds 数量 < threshold 时不触发决策）
  3. 恢复回退（_disable_module / restore_module 的异常路径）
  4. 窗口修剪（rounds 超 keep*4 时被截断）

设计原则：
  - 仅修改测试文件，不改动产品代码（``auto_disable.py``、``__init__.py``）。
  - 通过 ``unittest.mock`` 重写模块级路径函数，避开对真实 ComfyUI 的依赖。
  - 每个测试用临时目录隔离文件系统副作用，互不污染。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

# 让 ``import auto_disable`` 能找到仓库根目录下的模块
import sys

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

    def _known(self, mapping):
        """把 ``{name: [class_a, class_b]}`` 转成 known_modules 形态。"""
        return {
            name: {"node_classes": list(classes), "module_path": ""}
            for name, classes in mapping.items()
        }

    def _state(self, threshold=3, rounds=None, exclude=None, known=None):
        s = auto_disable._default_state()
        s["threshold"] = threshold
        s["rounds"] = list(rounds or [])
        s["exclude"] = list(exclude or [])
        s["known_modules"] = (
            self._known(known) if known is not None else {}
        )
        return s

    def _round(self, *used_classes, ts=0.0):
        return {"timestamp": ts, "used_classes": sorted(set(used_classes))}


# ---------------------------------------------------------------------------
# 1. 阈值判定
# ---------------------------------------------------------------------------


class ThresholdJudgmentTests(_IsolatedTestBase):
    """阈值边界：threshold=0、负数、1、3 + exclude。"""

    def test_threshold_zero_disables_nothing(self):
        """threshold=0 时，``_decide`` 应直接返回空，永不触发禁用。"""
        s = self._state(
            threshold=0,
            rounds=[self._round("A1") for _ in range(10)],
            known={"mod_a": ["A1"]},
        )
        self.assertEqual(auto_disable._decide(s), [])

    def test_threshold_negative_disables_nothing(self):
        """threshold=-1（负数）也属于非法值，应直接返回空。"""
        s = self._state(
            threshold=-1,
            rounds=[self._round() for _ in range(10)],
            known={"mod_a": ["A1"]},
        )
        self.assertEqual(auto_disable._decide(s), [])

    def test_threshold_one_disables_immediately(self):
        """threshold=1 时，只要最近一轮未用即触发禁用。"""
        s = self._state(
            threshold=1,
            rounds=[self._round()],
            known={"mod_a": ["A1"], "mod_b": ["B1", "B2"]},
        )
        newly = auto_disable._decide(s)
        self.assertCountEqual(newly, ["mod_a", "mod_b"])

    def test_threshold_three_does_not_disable_after_two_unused_rounds(self):
        """threshold=3 时，2 轮未用不该触发禁用。"""
        s = self._state(
            threshold=3,
            rounds=[self._round() for _ in range(2)],
            known={"mod_a": ["A1"]},
        )
        self.assertEqual(auto_disable._decide(s), [])

    def test_threshold_three_disables_after_three_unused_rounds(self):
        """threshold=3 时，连续 3 轮未用即触发禁用。"""
        s = self._state(
            threshold=3,
            rounds=[self._round() for _ in range(3)],
            known={"mod_a": ["A1"]},
        )
        self.assertEqual(auto_disable._decide(s), ["mod_a"])

    def test_excluded_module_is_never_disabled(self):
        """在 ``exclude`` 中的模块应被跳过；不在的仍可被禁用。"""
        s = self._state(
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
        s = self._state(threshold=1, rounds=[self._round()])
        self.assertEqual(auto_disable._decide(s), [])

    def test_already_disabled_module_is_skipped(self):
        """已经在 ``disabled`` 里的模块不应再次进入决策。"""
        s = self._state(
            threshold=1,
            rounds=[self._round()],
            known={"mod_a": ["A1"]},
        )
        s["disabled"]["mod_a"] = {
            "original_path": "",
            "disabled_at": 0.0,
        }
        self.assertEqual(auto_disable._decide(s), [])


# ---------------------------------------------------------------------------
# 2. 轮次不足
# ---------------------------------------------------------------------------


class InsufficientRoundsTests(_IsolatedTestBase):
    """rounds 数量 < threshold 时不触发决策。"""

    def test_no_rounds_at_all_disables_nothing(self):
        s = self._state(threshold=3, rounds=[], known={"mod_a": ["A1"]})
        self.assertEqual(auto_disable._decide(s), [])

    def test_rounds_less_than_threshold_disables_nothing(self):
        s = self._state(
            threshold=3,
            rounds=[self._round()],
            known={"mod_a": ["A1"]},
        )
        self.assertEqual(auto_disable._decide(s), [])

    def test_rounds_equal_to_threshold_with_no_usage_disables(self):
        """rounds 数量刚好等于 threshold 且无使用时触发禁用。"""
        s = self._state(
            threshold=3,
            rounds=[self._round() for _ in range(3)],
            known={"mod_a": ["A1"]},
        )
        self.assertEqual(auto_disable._decide(s), ["mod_a"])

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
        s = self._state(threshold=3, rounds=rounds, known={"mod_a": ["A1"]})
        self.assertEqual(auto_disable._decide(s), [])

    def test_partially_used_module_not_disabled(self):
        """只要某节点类出现在最近窗口内，整个模块都不应被禁用。"""
        rounds = [
            self._round("A1"),
            self._round(),
            self._round(),
        ]
        s = self._state(threshold=3, rounds=rounds, known={"mod_a": ["A1", "A2"]})
        self.assertEqual(auto_disable._decide(s), [])


# ---------------------------------------------------------------------------
# 3. 恢复回退
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
        # 1) 在 custom_nodes 下创建一个模块目录
        mod_path = os.path.join(self.custom_nodes_dir, "mod_a")
        os.makedirs(mod_path)
        with open(os.path.join(mod_path, "marker.py"), "w", encoding="utf-8") as f:
            f.write("# marker")

        # 2) 通过 _decide 触发实际移动
        s = self._state(
            threshold=1,
            rounds=[self._round()],
            known={"mod_a": ["A1"]},
        )
        s["known_modules"]["mod_a"]["module_path"] = mod_path
        auto_disable._save_state(s)
        auto_disable._decide(s)
        auto_disable._save_state(s)

        # 模块应已被移到 .disabled/
        moved = os.path.join(self.disabled_dir, "mod_a")
        self.assertTrue(os.path.isdir(moved))
        self.assertFalse(os.path.exists(mod_path))
        # state 记录里也应出现
        self.assertIn("mod_a", s["disabled"])

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
        }
        auto_disable._save_state(s)

        self.assertFalse(auto_disable.restore_module("mod_a"))

        # state 记录仍存在
        s2 = auto_disable._load_state()
        self.assertIn("mod_a", s2.get("disabled", {}))


# ---------------------------------------------------------------------------
# 4. 窗口修剪
# ---------------------------------------------------------------------------


class WindowPruningTests(_IsolatedTestBase):
    """``record_prompt`` 中 ``rounds`` 超 ``keep * 4`` 时被截断。"""

    def _keep(self, threshold: int) -> int:
        return max(int(threshold) + 2, 5)

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
        """未超过 ``keep*4`` 时不应修剪（负面验证：捕获潜在回归）。"""
        threshold = 3
        keep = self._keep(threshold)  # = 5
        # 刚好 keep*4 条：不超限
        n = keep * 4
        s = self._state(threshold=threshold, rounds=[
            self._round(ts=float(i)) for i in range(n)
        ])
        auto_disable._save_state(s)

        auto_disable.record_prompt([])

        new_state = auto_disable._load_state()
        # append 后变为 n + 1 = keep*4 + 1，未超限应保留全部
        self.assertEqual(len(new_state["rounds"]), n + 1)


# ---------------------------------------------------------------------------
# 刻意构造的失败用例（用于展示测试网格的捕获能力）
# ---------------------------------------------------------------------------


class DeliberateFailureDetectionTests(_IsolatedTestBase):
    """通过反向断言验证：若产品行为回归，本网格的测试能立即捕获。"""

    def test_threshold_zero_short_circuit(self):
        """若 ``_decide`` 在 threshold=0 时错误地尝试扫描，应被本测试捕获。"""
        s = self._state(
            threshold=0,
            rounds=[self._round() for _ in range(5)],
            known={"mod_a": ["A1"]},
        )
        # 负面断言：阈值=0 时不应有任何产出
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
        """验证 keep*4 这一边界：刚好等于上界时不应修剪。"""
        threshold = 3
        keep = self._keep(threshold)  # = 5
        # 关键边界：刚好 keep*4 条 → 等于上限 → 不应触发修剪
        n = keep * 4
        s = self._state(threshold=threshold, rounds=[
            self._round(ts=float(i)) for i in range(n)
        ])
        auto_disable._save_state(s)

        auto_disable.record_prompt([])

        rounds = auto_disable._load_state()["rounds"]
        self.assertEqual(
            len(rounds),
            n + 1,
            "刚好等于 keep*4 时不应修剪；append 后应为 keep*4 + 1。",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)