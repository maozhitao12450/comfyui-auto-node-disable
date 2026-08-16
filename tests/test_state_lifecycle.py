"""状态文件生命周期相关测试。

涵盖：
1. 启动对账 ``_reconcile_disabled_with_disk`` 的四种场景（state vs 磁盘）：
   一致 / 磁盘独有条目补齐 / state 独有但原始路径已恢复 → 删除 / 孤儿警告保留
2. 状态文件位置迁移：ComfyUI 根目录 → 插件目录，兼容旧位置

其余维度见兄弟文件：
- 阈值判定与窗口修剪 → ``tests/test_auto_disable.py``
- 恢复路径与缺失节点自动恢复 → ``tests/test_recovery.py``
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
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
        """磁盘上 .disabled/ 有但 state 里没有时，应追加到 state。

        本测试场景下被扫描的“模块”是一个空目录（没有 ``__init__.py``），
        reconcile 拿不到 ``NODE_CLASS_MAPPINGS``，``node_classes`` 应为空列表，
        并打印一条 WARNING 提示类名扫描失败。
        """
        os.makedirs(self.disabled_dir, exist_ok=True)
        # 手动在磁盘上建一个禁用模块（空目录 → 扫不到类名）
        os.makedirs(os.path.join(self.disabled_dir, "external_mod"), exist_ok=True)
        s = auto_disable._default_state()
        auto_disable._save_state(s)

        result = auto_disable._reconcile_disabled_with_disk(s)

        self.assertTrue(result["changed"])
        self.assertEqual(result["added"], ["external_mod"])
        self.assertEqual(result["restored"], [])
        # state 应被补齐；node_classes 因扫描失败为空列表
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

    def test_disk_only_entry_scans_node_classes_when_importable(self):
        """磁盘上的 disabled 模块若能 import 提取到 ``NODE_CLASS_MAPPINGS``，
        reconcile 写入 state 时应带上扫描到的类名列表。
        """
        os.makedirs(self.disabled_dir, exist_ok=True)
        # 造一个“被禁用”的 custom_node 目录，带 __init__.py + NODE_CLASS_MAPPINGS
        mod_dir = os.path.join(self.disabled_dir, "scannable_mod")
        os.makedirs(mod_dir, exist_ok=True)
        with open(os.path.join(mod_dir, "__init__.py"), "w", encoding="utf-8") as f:
            f.write(
                "NODE_CLASS_MAPPINGS = {'ScanA': object(), 'ScanB': object(), 'ScanC': object()}\n"
            )
        s = auto_disable._default_state()
        auto_disable._save_state(s)

        result = auto_disable._reconcile_disabled_with_disk(s)

        self.assertTrue(result["changed"])
        self.assertEqual(result["added"], ["scannable_mod"])
        info = s["disabled"]["scannable_mod"]
        # reconcile 应在 reconcile 内扫到类名
        self.assertEqual(sorted(info["node_classes"]), ["ScanA", "ScanB", "ScanC"])

    def test_disk_only_entry_with_failing_import_does_not_crash(self):
        """import 期间出错（依赖缺失、语法错）的 disabled 模块不应阻塞 reconcile。"""
        os.makedirs(self.disabled_dir, exist_ok=True)
        mod_dir = os.path.join(self.disabled_dir, "broken_mod")
        os.makedirs(mod_dir, exist_ok=True)
        # 语法错 + import 不存在的依赖
        with open(os.path.join(mod_dir, "__init__.py"), "w", encoding="utf-8") as f:
            f.write("import this_module_definitely_does_not_exist_xyz\n")
        s = auto_disable._default_state()
        auto_disable._save_state(s)

        # 不应抛异常
        result = auto_disable._reconcile_disabled_with_disk(s)

        self.assertTrue(result["changed"])
        self.assertEqual(result["added"], ["broken_mod"])
        # node_classes 为空列表（scan 失败）但 state 仍然补齐了
        self.assertIn("broken_mod", s["disabled"])
        self.assertEqual(s["disabled"]["broken_mod"]["node_classes"], [])

    def test_extract_node_classes_from_path_returns_none_for_missing(self):
        """_extract_node_classes_from_path 对不存在路径应返回 None。"""
        self.assertIsNone(
            auto_disable._extract_node_classes_from_path(
                os.path.join(self.tmpdir, "no_such_path")
            )
        )

    def test_extract_node_classes_from_path_returns_list_for_real_module(self):
        """_extract_node_classes_from_path 能从带 NODE_CLASS_MAPPINGS 的
        __init__.py 提取类名。"""
        mod_dir = os.path.join(self.tmpdir, "sample_mod")
        os.makedirs(mod_dir, exist_ok=True)
        with open(os.path.join(mod_dir, "__init__.py"), "w", encoding="utf-8") as f:
            f.write(
                "NODE_CLASS_MAPPINGS = {'Alpha': object(), 'Beta': object()}\n"
            )
        result = auto_disable._extract_node_classes_from_path(mod_dir)
        self.assertEqual(result, ["Alpha", "Beta"])

    def test_extract_node_classes_from_path_returns_empty_list_for_no_mapping(self):
        """__init__.py 加载成功但未声明 NODE_CLASS_MAPPINGS → 返回空列表
        （与加载失败返回 None 区分）。"""
        mod_dir = os.path.join(self.tmpdir, "no_mapping_mod")
        os.makedirs(mod_dir, exist_ok=True)
        with open(os.path.join(mod_dir, "__init__.py"), "w", encoding="utf-8") as f:
            f.write("# nothing here\n")
        result = auto_disable._extract_node_classes_from_path(mod_dir)
        self.assertEqual(result, [])

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
# 4c. 状态文件位置迁移：ComfyUI 根目录 → 插件目录
# ---------------------------------------------------------------------------


class StateLocationMigrationTests(unittest.TestCase):
    """状态文件从 ``ComfyUI/auto_node_disable_state.json`` 迁到插件目录内。

    不复用 ``_IsolatedTestBase``，因为它会把 ``_comfy_root`` 打桩到 ``tmpdir``，
    使 legacy 与 new 路径退化为同一路径，无法验证迁移。
    本类手动将 ``_comfy_root`` 与 ``_state_path`` 指向不同子目录。
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="auto_disable_migrate_")
        # 旧位置：ComfyUI 根目录（JSON 后缀）
        self.legacy_root = os.path.join(self.tmpdir, "ComfyUI_old")
        self.legacy_path = os.path.join(
            self.legacy_root, auto_disable.LEGACY_STATE_FILENAME
        )
        # 新位置：插件目录（SQLite 后缀，跟随现行 STATE_FILENAME）
        self.plugin_dir = os.path.join(self.tmpdir, "plugin")
        self.new_path = os.path.join(
            self.plugin_dir, auto_disable.STATE_FILENAME
        )
        os.makedirs(self.legacy_root, exist_ok=True)
        os.makedirs(self.plugin_dir, exist_ok=True)

        self._patches = [
            mock.patch.object(
                auto_disable, "_comfy_root", return_value=self.legacy_root
            ),
            mock.patch.object(
                auto_disable, "_state_path", return_value=self.new_path
            ),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_legacy(self, payload: dict) -> None:
        with open(self.legacy_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def _read_db_dict(self, db_path: str) -> dict:
        """从任意路径的 SQLite 文件读取 state dict（不依赖 ``self.state_file``）。"""
        import sqlite3 as _sqlite3
        with _sqlite3.connect(db_path) as conn:
            conn.row_factory = _sqlite3.Row
            # settings 阈值是必看字段，其它字段（known/rounds/disabled/pending_restart）
            # 如果不存在则取默认值，与产品代码的默认值保持一致。
            state = {
                "threshold": auto_disable.DEFAULT_THRESHOLD,
                "dry_run": False,
                "exclude": list(auto_disable.DEFAULT_EXCLUDE),
                "known_modules": {},
                "rounds": [],
                "disabled": {},
                "pending_restart": [],
            }
            row = conn.execute(
                "SELECT key, value FROM settings WHERE key = ?", ("threshold",)
            ).fetchone()
            if row is not None:
                try:
                    state["threshold"] = int(row["value"])
                except (ValueError, TypeError):
                    pass
            row = conn.execute(
                "SELECT key, value FROM settings WHERE key = ?", ("dry_run",)
            ).fetchone()
            if row is not None:
                state["dry_run"] = row["value"] in ("1", "true", "True")
            row = conn.execute(
                "SELECT key, value FROM settings WHERE key = ?", ("exclude",)
            ).fetchone()
            if row is not None:
                try:
                    state["exclude"] = json.loads(row["value"])
                except Exception:
                    pass
            extra_rows = conn.execute(
                "SELECT key, value FROM settings WHERE key NOT IN "
                "('threshold', 'dry_run', 'exclude')"
            ).fetchall()
            for r in extra_rows:
                state[r["key"]] = r["value"]
            return state

    def test_legacy_is_migrated_to_plugin_dir(self):
        """旧位置存在且新位置不存在时，迁移后旧 JSON 被归档、新位置变成 SQLite 且内容一致。"""
        self._write_legacy({"threshold": 7})
        self.assertTrue(os.path.exists(self.legacy_path))
        self.assertFalse(os.path.exists(self.new_path))

        auto_disable._migrate_legacy_state()

        # 旧 JSON 被归档为 .json.migrated，不再以原名存在
        self.assertFalse(os.path.exists(self.legacy_path))
        self.assertTrue(os.path.exists(self.legacy_path + ".migrated"))
        # 新位置现在是 SQLite 数据库文件
        self.assertTrue(os.path.exists(self.new_path))
        # 读 SQLite 验证 threshold 被正确迁移
        persisted = self._read_db_dict(self.new_path)
        self.assertEqual(persisted["threshold"], 7)

    def test_new_kept_when_both_exist(self):
        """新旧都存在时，新 SQLite 为准；旧 JSON 被归档为 .migrated，不覆盖新文件。"""
        self._write_legacy({"threshold": 1})
        # 新位置预填一份 SQLite（threshold=9），代表“已有状态”场景
        auto_disable._storage.save_state_to_db(
            self.new_path,
            {
                "threshold": 9,
                "dry_run": False,
                "exclude": [],
                "known_modules": {},
                "rounds": [],
                "disabled": {},
                "pending_restart": [],
            },
        )

        auto_disable._migrate_legacy_state()

        # 旧 JSON 被归档为 .migrated
        self.assertFalse(os.path.exists(self.legacy_path))
        self.assertTrue(os.path.exists(self.legacy_path + ".migrated"))
        # 新 SQLite 仍在且未被覆盖
        self.assertTrue(os.path.exists(self.new_path))
        persisted = self._read_db_dict(self.new_path)
        self.assertEqual(persisted["threshold"], 9)

    def test_no_op_when_legacy_missing(self):
        """旧位置不存在时，迁移是 no-op（不创建新文件）。"""
        self.assertFalse(os.path.exists(self.legacy_path))

        auto_disable._migrate_legacy_state()

        self.assertFalse(os.path.exists(self.new_path))

    def test_load_state_triggers_migration(self):
        """``_load_state`` 调用时自动迁移旧位置 -> 插件目录（JSON 归档 + SQLite 落地）。"""
        self._write_legacy({"threshold": 11})

        loaded = auto_disable._load_state()

        # 旧 JSON 被归档为 .migrated，新位置变 SQLite
        self.assertFalse(os.path.exists(self.legacy_path))
        self.assertTrue(os.path.exists(self.legacy_path + ".migrated"))
        self.assertTrue(os.path.exists(self.new_path))
        # 加载时应能读到旧配置
        self.assertEqual(loaded["threshold"], 11)

    def test_legacy_path_is_comfy_root_state(self):
        """验证 ``_legacy_state_path`` 拼出的是 ComfyUI 根目录下的旧 JSON 文件名。"""
        expected = os.path.join(
            self.legacy_root, auto_disable.LEGACY_STATE_FILENAME
        )
        self.assertEqual(auto_disable._legacy_state_path(), expected)

    def test_state_path_is_plugin_dir_state(self):
        """验证 ``_state_path`` 拼出的是插件目录下的文件名。"""
        expected = os.path.join(self.plugin_dir, auto_disable.STATE_FILENAME)
        self.assertEqual(auto_disable._state_path(), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)