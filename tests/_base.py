"""测试共享基类。

把 ``auto_disable`` 的所有磁盘路径与扫描副作用重定向到临时目录，避免
依赖真实 ComfyUI 与磁盘布局。被 ``tests/test_auto_disable.py``、
``tests/test_recovery.py``、``tests/test_state_lifecycle.py`` 共享。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
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