"""known_modules 同步语义测试。

覆盖以下行为（与 2026-08-17 改动配套）：

1. **缺失节点类自动恢复时回填 known_modules**：
   - ``restore_for_missing_classes`` 把模块移回 ``custom_nodes/`` 后，
     必须立即把该模块加入 ``state["known_modules"]``，确保下一轮
     ``_decide`` 把它视为"已知且当前可用"。
2. **物理 disable 成功后从 known_modules 移除**：
   - ``_decide`` 把模块物理移到 ``.disabled/`` 后，从 ``known_modules``
     移除，避免下次扫描又把"已禁用"模块视为待禁用候选。
3. **record_prompt 每次都刷新 known_modules**：
   - ``record_prompt`` 在每次入队时调 ``_scan_known_modules``，
   - 用户明确要求：known_modules 与磁盘现状始终保持一致。
4. **refresh_known_modules 手动刷新**：
   - 调用 ``refresh_known_modules`` 后 ``state["known_modules"]`` 反映
     最新一次 ``NODE_CLASS_MAPPINGS`` 扫描结果。
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest
from unittest import mock

import auto_disable

# 让 ``_base`` / ``auto_disable`` 都能以模块方式被定位
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(TESTS_DIR, os.pardir))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

from _base import _IsolatedTestBase  # noqa: E402


class RestoreAddsToKnownModulesTests(_IsolatedTestBase):
    """``restore_for_missing_classes`` 恢复成功后回填 known_modules。"""

    def test_restored_module_added_to_known_modules(self):
        """当缺失类被某个 disabled 模块补齐、且恢复成功时，该模块应加入 known_modules。"""
        # 真实建一个目录作为将被恢复的模块
        mod_path = self._make_module("restorable_mod")
        # 把它物理移到 .disabled/，模拟"已被自动禁用"
        os.makedirs(self.disabled_dir, exist_ok=True)
        disabled_path = os.path.join(self.disabled_dir, "restorable_mod")
        shutil.move(mod_path, disabled_path)

        s = self._state(threshold=3, rounds=[])
        s["disabled"] = {
            "restorable_mod": {
                "original_path": mod_path,
                "disabled_at": 0.0,
                "status": "confirmed",
                "node_classes": ["RestorableClass"],
            },
        }
        # known_modules 故意为空，验证恢复后会自动回填
        s["known_modules"] = {}
        auto_disable._save_state(s)

        # 模拟"本次入队用了 RestorableClass，但当前进程内未注册"
        with mock.patch.object(
            auto_disable, "_current_registered_classes",
            return_value={"OtherClass"},
        ):
            restored = auto_disable.restore_for_missing_classes(
                s, ["RestorableClass"], prompt_id="p1"
            )

        self.assertEqual(restored, ["restorable_mod"])
        # 关键断言：被恢复的模块必须已加入 known_modules
        self.assertIn("restorable_mod", s["known_modules"])
        self.assertEqual(
            s["known_modules"]["restorable_mod"]["node_classes"], ["RestorableClass"]
        )

    def test_existing_known_module_not_overwritten_on_restore(self):
        """恢复时如果 known_modules 已有该模块，不应覆盖其路径/类信息。"""
        mod_path = self._make_module("preserve_mod")
        os.makedirs(self.disabled_dir, exist_ok=True)
        shutil.move(mod_path, os.path.join(self.disabled_dir, "preserve_mod"))

        original_path = "/some/canonical/path/preserve_mod"
        s = self._state(threshold=3, rounds=[])
        s["disabled"] = {
            "preserve_mod": {
                "original_path": original_path,
                "disabled_at": 0.0,
                "status": "confirmed",
                "node_classes": ["PreserveClass"],
            },
        }
        # 已知条目用不同的路径信息
        s["known_modules"] = {
            "preserve_mod": {
                "node_classes": ["PreserveClass"],
                "module_path": original_path,
            },
        }
        auto_disable._save_state(s)

        with mock.patch.object(
            auto_disable, "_current_registered_classes",
            return_value={"OtherClass"},
        ):
            auto_disable.restore_for_missing_classes(s, ["PreserveClass"])

        # 原条目保持不变
        self.assertEqual(
            s["known_modules"]["preserve_mod"]["module_path"], original_path
        )
        self.assertEqual(
            s["known_modules"]["preserve_mod"]["node_classes"], ["PreserveClass"]
        )


class DecideRemovesFromKnownModulesTests(_IsolatedTestBase):
    """``_decide`` 物理 disable 成功后从 known_modules 移除模块。"""

    def test_physically_disabled_module_removed_from_known(self):
        """物理 disable 成功后，模块从 ``state["known_modules"]`` 移除。"""
        mod_path = self._make_module("vanish_mod")
        # 构造一轮使用"无关类"的记录；threshold=1 时 recent 就是这一轮，
        # ``VanishClass`` 不在 used_union 中，``_decide`` 会把 vanish_mod 禁用。
        s = self._state(threshold=1, rounds=[
            {"timestamp": 0.0, "used_classes": ["SomeOtherClass"], "prompt_id": "p0"},
        ])
        s["known_modules"] = {
            "vanish_mod": {
                "node_classes": ["VanishClass"],
                "module_path": mod_path,
            },
        }
        auto_disable._save_state(s)

        newly = auto_disable._decide(s, last_prompt_id="p1")

        self.assertEqual(newly, ["vanish_mod"])
        # 物理 disable 成功后 known_modules 应不再包含该模块
        self.assertNotIn("vanish_mod", s["known_modules"])
        # 已被物理移到 .disabled/
        self.assertTrue(
            os.path.isdir(os.path.join(self.disabled_dir, "vanish_mod")),
            "vanish_mod 应被物理移到 .disabled/",
        )

    def test_dry_run_keeps_known_module(self):
        """``dry_run=True`` 时不应从 known_modules 移除：模块未被物理移走，
        下一次 record_prompt 仍要把它作为"待禁用候选"观察。"""
        mod_path = self._make_module("dry_keep_mod")
        s = self._dry_state(threshold=1, rounds=[
            {"timestamp": 0.0, "used_classes": ["SomeOtherClass"], "prompt_id": "p0"},
        ])
        s["known_modules"] = {
            "dry_keep_mod": {
                "node_classes": ["DryKeepClass"],
                "module_path": mod_path,
            },
        }
        auto_disable._save_state(s)

        newly = auto_disable._decide(s, last_prompt_id="p1")

        self.assertEqual(newly, ["dry_keep_mod"])
        # dry_run：模块应仍在 known_modules 里
        self.assertIn("dry_keep_mod", s["known_modules"])
        # dry_run：不移动目录
        self.assertTrue(
            os.path.isdir(mod_path),
            "dry_run 下不应移动目录",
        )

    def test_rollback_keeps_known_module(self):
        """物理移动失败回滚时，模块应保留在 known_modules。"""
        s = self._state(threshold=1, rounds=[
            {"timestamp": 0.0, "used_classes": ["SomeOtherClass"], "prompt_id": "p0"},
        ])
        # module_path 指向不存在的路径，_disable_module 会失败
        s["known_modules"] = {
            "ghost_path_mod": {
                "node_classes": ["GhostPathClass"],
                "module_path": os.path.join(self.custom_nodes_dir, "ghost_path_mod"),
            },
        }
        auto_disable._save_state(s)

        newly = auto_disable._decide(s, last_prompt_id="p1")

        self.assertEqual(newly, [])
        # 移动失败 → known_modules 应保留
        self.assertIn("ghost_path_mod", s["known_modules"])
        self.assertNotIn("ghost_path_mod", s["disabled"])


class RecordPromptRescanTests(_IsolatedTestBase):
    """``record_prompt`` 每次入队都重新刷新 ``known_modules``。"""

    def test_record_prompt_invokes_scan_each_time(self):
        """每次 ``record_prompt`` 调用都会触发 ``_scan_known_modules``。

        这是用户明确要求的设计：让 ``known_modules`` 与磁盘现状始终保持一致，
        避免“启动后新装/卸载模块”与 ``known_modules`` 脱节。
        """
        scan_calls = {"count": 0}

        def counting_scan(state):
            scan_calls["count"] += 1
            return state

        with mock.patch.object(
            auto_disable, "_scan_known_modules", side_effect=counting_scan
        ):
            auto_disable.record_prompt(["A1"])
            auto_disable.record_prompt(["B2"])
            auto_disable.record_prompt(["C3"])

        self.assertEqual(
            scan_calls["count"], 3,
            "每次 record_prompt 都应调用 _scan_known_modules；"
            f"实际调用了 {scan_calls['count']} 次",
        )


class RefreshKnownModulesTests(_IsolatedTestBase):
    """``refresh_known_modules`` 手动刷新入口。"""

    def test_refresh_replaces_known_modules(self):
        """``refresh_known_modules`` 应重新扫描并覆盖 ``known_modules``。"""
        # 初始状态含一个旧模块
        s = self._state(threshold=3, rounds=[])
        s["known_modules"] = {
            "old_mod": {"node_classes": ["OldClass"], "module_path": "/old/path"},
        }
        auto_disable._save_state(s)

        new_known = {
            "new_mod_alpha": {
                "node_classes": ["NewAlphaClass"],
                "module_path": "/new/alpha",
            },
            "new_mod_beta": {
                "node_classes": ["NewBetaClass"],
                "module_path": "/new/beta",
            },
        }

        with mock.patch.object(
            auto_disable, "_scan_known_modules",
            side_effect=lambda state: state.update(
                {"known_modules": {
                    name: {
                        "node_classes": list(info["node_classes"]),
                        "module_path": info["module_path"],
                    }
                    for name, info in new_known.items()
                }}
            ) or state["known_modules"],
        ):
            result = auto_disable.refresh_known_modules()

        # 返回的视图应反映扫描结果
        self.assertIn("new_mod_alpha", result)
        self.assertIn("new_mod_beta", result)
        self.assertNotIn("old_mod", result)

        # state["known_modules"] 也应被覆盖并持久化
        reloaded = auto_disable._load_state()
        self.assertIn("new_mod_alpha", reloaded["known_modules"])
        self.assertIn("new_mod_beta", reloaded["known_modules"])
        self.assertNotIn("old_mod", reloaded["known_modules"])


if __name__ == "__main__":
    unittest.main()
