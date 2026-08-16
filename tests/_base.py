"""测试共享基类。

把 ``auto_disable`` 的所有磁盘路径与扫描副作用重定向到临时目录，避免
依赖真实 ComfyUI 与磁盘布局。被 ``tests/test_auto_disable.py``、
``tests/test_recovery.py``、``tests/test_state_lifecycle.py`` 共享。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from typing import Any
from unittest import mock

# 把仓库根与 ``tests/`` 自身加入 sys.path，使 ``_base`` 能作为同目录模块
# 被测试文件 ``from _base import _IsolatedTestBase`` 加载；同时让
# ``import auto_disable`` 走包路径而非相对路径，与原文件一致。
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(TESTS_DIR, os.pardir))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

import auto_disable  # noqa: E402


class _IsolatedTestBase(unittest.TestCase):
    """把 ``auto_disable`` 的所有磁盘路径重定向到 ``tmpdir``，并屏蔽 ``nodes`` 导入。"""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="auto_disable_test_")
        self.custom_nodes_dir = os.path.join(self.tmpdir, "custom_nodes")
        self.disabled_dir = os.path.join(self.custom_nodes_dir, ".disabled")
        # 状态文件后缀走产品代码常量，这样从 JSON 迁到 SQLite 后测试无需调整
        self.state_file = os.path.join(self.tmpdir, auto_disable.STATE_FILENAME)
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
        """在 ``custom_nodes/`` 下创建模块目录并写入标记文件，返回模块路径。"""
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
        """开启 ``dry_run`` 的状态：``_decide`` 只写审计字段、不移动目录。"""
        s = self._state(threshold, rounds, exclude, known)
        s["dry_run"] = True
        return s

    def _known(self, mapping):
        """把 ``{name: [classes]}`` 转成 ``known_modules`` 形态（路径为空）。"""
        return {
            name: {"node_classes": list(classes), "module_path": ""}
            for name, classes in mapping.items()
        }

    def _round(self, *used_classes, ts=0.0):
        return {"timestamp": ts, "used_classes": sorted(set(used_classes))}

    # ----- 状态文件读写 -----

    def _write_state_raw(self, payload: dict[str, Any]) -> None:
        """直接走 ``_storage.save_state_to_db`` 写入一份 state，绕过业务逻辑。

        用于“模拟旧版状态 / 手工构造测试场景”。
        """
        auto_disable._storage.save_state_to_db(self.state_file, payload)

    def _read_state_raw(self) -> dict[str, Any]:
        """直接从 SQLite 文件读取整份 state dict。"""
        if not os.path.exists(self.state_file):
            return {}
        with sqlite3.connect(self.state_file) as conn:
            conn.row_factory = sqlite3.Row
            state: dict[str, Any] = {
                "threshold": auto_disable.DEFAULT_THRESHOLD,
                "dry_run": False,
                "exclude": [],
                "known_modules": {},
                "rounds": [],
                "disabled": {},
                "pending_restart": [],
            }
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
            settings = {r["key"]: r["value"] for r in rows}
            if "threshold" in settings:
                try:
                    state["threshold"] = int(settings["threshold"])
                except (ValueError, TypeError):
                    pass
            if "dry_run" in settings:
                state["dry_run"] = settings["dry_run"] in ("1", "true", "True")
            if "exclude" in settings:
                try:
                    state["exclude"] = json.loads(settings["exclude"])
                except Exception:
                    state["exclude"] = []
            rows = conn.execute(
                "SELECT module_name, node_classes, module_path FROM known_modules"
            ).fetchall()
            for r in rows:
                try:
                    classes = json.loads(r["node_classes"])
                except Exception:
                    classes = []
                state["known_modules"][r["module_name"]] = {
                    "node_classes": classes,
                    "module_path": r["module_path"] or "",
                }
            rows = conn.execute(
                "SELECT timestamp, used_classes, prompt_id FROM rounds ORDER BY id"
            ).fetchall()
            for r in rows:
                try:
                    classes = json.loads(r["used_classes"])
                except Exception:
                    classes = []
                entry: dict[str, Any] = {
                    "timestamp": r["timestamp"],
                    "used_classes": classes,
                }
                if r["prompt_id"]:
                    entry["prompt_id"] = r["prompt_id"]
                state["rounds"].append(entry)
            rows = conn.execute(
                "SELECT module_name, original_path, disabled_at, prompt_id, "
                "node_classes, status FROM disabled"
            ).fetchall()
            for r in rows:
                info: dict[str, Any] = {
                    "original_path": r["original_path"] or "",
                    "disabled_at": r["disabled_at"],
                    "status": r["status"],
                }
                if r["prompt_id"]:
                    info["prompt_id"] = r["prompt_id"]
                if r["node_classes"]:
                    try:
                        info["node_classes"] = json.loads(r["node_classes"])
                    except Exception:
                        info["node_classes"] = []
                state["disabled"][r["module_name"]] = info
            rows = conn.execute(
                "SELECT module, node_classes, restored_at, prompt_id "
                "FROM pending_restart ORDER BY id"
            ).fetchall()
            for r in rows:
                try:
                    classes = json.loads(r["node_classes"])
                except Exception:
                    classes = []
                entry = {
                    "module": r["module"],
                    "node_classes": classes,
                    "restored_at": r["restored_at"],
                }
                if r["prompt_id"]:
                    entry["prompt_id"] = r["prompt_id"]
                state["pending_restart"].append(entry)
            return state