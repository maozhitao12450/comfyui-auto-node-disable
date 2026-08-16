"""恢复路径相关测试。

涵盖：
1. ``_disable_module`` / ``restore_module`` 在异常路径下的回退行为
   （模块路径不存在 / 目标已存在 / pending 状态对账）
2. 缺失节点类自动恢复：submit 引用未注册类 → ``.disabled`` 匹配 → 原子恢复 →
   ``pending_restart`` 提示与消费；含拒绝/忽略/原子化等边界

其余维度见兄弟文件：
- 阈值判定与窗口修剪 → ``tests/test_auto_disable.py``
- 启动对账与状态迁移 → ``tests/test_state_lifecycle.py``
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

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
        persisted = self._read_state_raw()
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
        persisted = self._read_state_raw()
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
        persisted = self._read_state_raw()
        self.assertIn("mod_a", persisted["disabled"])
        self.assertEqual(persisted.get("pending_restart", []), [])

    def test_pending_restart_defaults_when_missing_in_old_state_file(self):
        """旧版本状态文件缺 pending_restart 字段时，加载后应被补齐为 []。"""
        legacy = auto_disable._default_state()
        legacy.pop("pending_restart", None)
        # 走 SQLite 写入模拟“旧结构”（缺字段）的状态文件
        self._write_state_raw(legacy)
        loaded = auto_disable._load_state()
        self.assertEqual(loaded.get("pending_restart"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)