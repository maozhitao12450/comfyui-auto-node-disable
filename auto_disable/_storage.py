"""SQLite 持久化层。

替代旧的 JSON 文件存储：

- 全量覆盖写入：``save_state_to_db`` 把整个 state dict 重新写入所有表（事务）；
- 全量读取：``load_state_from_db`` 把所有表拼回 state dict；
- 旧版 JSON 文件 ``auto_node_disable_state.json`` 通过 ``migrate_json_to_db``
  在首次启动时自动导入数据库并归档为 ``.json.migrated``。

原子化
------
``DELETE + INSERT`` 包装在显式事务里，``WAL`` 模式确保崩溃后能恢复，
``synchronous=NORMAL`` 兼顾性能与安全；任何 INSERT 失败都触发 ROLLBACK，
避免半写状态。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any

import auto_disable


# 当前 schema 版本；后续加表/字段时升级此值并写迁移分支
SCHEMA_VERSION = 1


def init_db(db_path: str) -> None:
    """确保数据库存在且 schema 是当前版本。幂等可重入。

    不会清空已有数据；只在表不存在时建表、首次启动写入 ``schema_version``。
    """
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with _connect(db_path) as conn:
        c = conn.cursor()
        c.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "version INTEGER PRIMARY KEY"
            ")"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS settings ("
            "key TEXT PRIMARY KEY, "
            "value TEXT"
            ")"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS known_modules ("
            "module_name TEXT PRIMARY KEY, "
            "node_classes TEXT NOT NULL, "  # JSON array
            "module_path TEXT NOT NULL DEFAULT ''"
            ")"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS rounds ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp REAL NOT NULL, "
            "used_classes TEXT NOT NULL, "  # JSON array
            "prompt_id TEXT"
            ")"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS disabled ("
            "module_name TEXT PRIMARY KEY, "
            "original_path TEXT NOT NULL DEFAULT '', "
            "disabled_at REAL NOT NULL, "
            "prompt_id TEXT, "
            "node_classes TEXT, "  # JSON array, nullable
            "status TEXT NOT NULL"
            ")"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS pending_restart ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "module TEXT NOT NULL, "
            "node_classes TEXT NOT NULL, "  # JSON array
            "restored_at REAL NOT NULL, "
            "prompt_id TEXT"
            ")"
        )
        # 写 schema_version（仅首次）
        row = c.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        if row is None:
            c.execute(
                "INSERT INTO schema_version(version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
        elif int(row[0]) != SCHEMA_VERSION:
            # 未来版本升级点：先 raise 让调用方决定如何迁移
            raise RuntimeError(
                f"unsupported schema version {row[0]} (expected {SCHEMA_VERSION})"
            )


def _connect(db_path: str) -> sqlite3.Connection:
    """建立 SQLite 连接（带 WAL 与合理 timeout）。"""
    conn = sqlite3.connect(db_path, timeout=10.0, isolation_level=None)  # autocommit
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def load_state_from_db(db_path: str) -> dict[str, Any]:
    """从 SQLite 加载整份 state dict；不存在则返回默认结构。

    任何读取异常（文件不存在、schema 损坏、字段缺失等）一律返回默认状态
    并记 warning，绝不抛出到 ``_load_state`` 调用方。
    """
    if not os.path.exists(db_path):
        return auto_disable._default_state()
    try:
        with _connect(db_path) as conn:
            return _read_all(conn)
    except Exception as e:
        auto_disable.log.warning(
            "auto_node_disable: db unreadable (%s); starting fresh", e
        )
        return auto_disable._default_state()


def _read_all(conn: sqlite3.Connection) -> dict[str, Any]:
    """从打开的连接把所有表读出来拼成 state dict。

    各表缺字段时回落到 ``_default_state()`` 的默认；``node_classes`` 反序列化
    失败时退化为空列表，避免阻塞整体加载。
    """
    state = auto_disable._default_state()

    # ---- settings ----
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
            pass

    # ---- known_modules ----
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

    # ---- rounds（按插入顺序）----
    rows = conn.execute(
        "SELECT timestamp, used_classes, prompt_id FROM rounds ORDER BY id"
    ).fetchall()
    for r in rows:
        try:
            classes = json.loads(r["used_classes"])
        except Exception:
            classes = []
        entry: dict[str, Any] = {"timestamp": r["timestamp"], "used_classes": classes}
        if r["prompt_id"]:
            entry["prompt_id"] = r["prompt_id"]
        state["rounds"].append(entry)

    # ---- disabled ----
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

    # ---- pending_restart ----
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

    # pending_restart 必须是 list（老结构兼容）
    if not isinstance(state["pending_restart"], list):
        state["pending_restart"] = []

    return state


def save_state_to_db(db_path: str, state: dict[str, Any]) -> None:
    """全量把 state dict 写入 SQLite（事务覆盖所有业务表）。

    使用 ``DELETE * FROM table`` 清空再 ``INSERT`` 重写，保证：
    - 旧字段被自然清除（避免遗留键）；
    - 所有写入在同一事务里完成，要么全部成功要么全部回滚；
    - ``known_modules / rounds / disabled / pending_restart`` 这类带序表
      维持 state dict 里的顺序。
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        c = conn.cursor()
        c.execute("BEGIN")
        try:
            # 清空业务表（保留 schema_version）
            c.execute("DELETE FROM settings")
            c.execute("DELETE FROM known_modules")
            c.execute("DELETE FROM rounds")
            c.execute("DELETE FROM disabled")
            c.execute("DELETE FROM pending_restart")

            # ---- settings ----
            threshold = int(state.get("threshold", auto_disable.DEFAULT_THRESHOLD))
            dry_run = bool(state.get("dry_run", False))
            exclude = json.dumps(
                list(state.get("exclude", []) or []),
                ensure_ascii=False,
            )
            c.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?)",
                ("threshold", str(threshold)),
            )
            c.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?)",
                ("dry_run", "1" if dry_run else "0"),
            )
            c.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?)",
                ("exclude", exclude),
            )

            # ---- known_modules ----
            known = state.get("known_modules") or {}
            for module_name, info in known.items():
                if not isinstance(info, dict):
                    continue
                classes = json.dumps(
                    list(info.get("node_classes", []) or []),
                    ensure_ascii=False,
                )
                path = info.get("module_path") or ""
                c.execute(
                    "INSERT INTO known_modules"
                    "(module_name, node_classes, module_path) "
                    "VALUES (?, ?, ?)",
                    (module_name, classes, path),
                )

            # ---- rounds ----
            rounds = state.get("rounds") or []
            for r in rounds:
                if not isinstance(r, dict):
                    continue
                ts = float(r.get("timestamp", time.time()))
                classes = json.dumps(
                    list(r.get("used_classes", []) or []),
                    ensure_ascii=False,
                )
                pid = r.get("prompt_id")
                c.execute(
                    "INSERT INTO rounds(timestamp, used_classes, prompt_id) "
                    "VALUES (?, ?, ?)",
                    (ts, classes, pid),
                )

            # ---- disabled ----
            disabled = state.get("disabled") or {}
            for module_name, info in disabled.items():
                if not isinstance(info, dict):
                    continue
                orig = info.get("original_path") or ""
                disabled_at = float(info.get("disabled_at", time.time()))
                pid = info.get("prompt_id")
                nc = info.get("node_classes")
                nc_str = (
                    json.dumps(list(nc), ensure_ascii=False)
                    if nc
                    else None
                )
                status = str(info.get("status", "confirmed"))
                c.execute(
                    "INSERT INTO disabled"
                    "(module_name, original_path, disabled_at, "
                    "prompt_id, node_classes, status) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (module_name, orig, disabled_at, pid, nc_str, status),
                )

            # ---- pending_restart ----
            pending = state.get("pending_restart") or []
            for p in pending:
                if not isinstance(p, dict):
                    continue
                module = p.get("module", "")
                classes = json.dumps(
                    list(p.get("node_classes", []) or []),
                    ensure_ascii=False,
                )
                ra = float(p.get("restored_at", time.time()))
                pid = p.get("prompt_id")
                c.execute(
                    "INSERT INTO pending_restart"
                    "(module, node_classes, restored_at, prompt_id) "
                    "VALUES (?, ?, ?, ?)",
                    (module, classes, ra, pid),
                )

            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def migrate_json_to_db(json_path: str, db_path: str) -> bool:
    """把旧版 JSON 状态文件导入 SQLite 并归档原文件。

    - json_path 不存在 → no-op，返回 False；
    - json_path 内容损坏 → 记 warning 并返回 False（保留旧文件供人工处理）；
    - db_path 已存在 → 以新为准，**跳过数据导入**（避免覆盖用户已有状态），
      旧 JSON 文件仍被归档为 ``.migrated``；
    - 迁移成功后把原文件 ``os.replace`` 为 ``<json_path>.migrated``，
      避免下次启动重复处理；
    - 即使归档失败也不影响 SQLite 落地（用户数据已在新位置）。

    返回 True 表示本次确实完成了导入（数据写入）。
    """
    if not os.path.exists(json_path):
        return False
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        auto_disable.log.warning(
            "auto_node_disable: failed to read legacy JSON %s: %s",
            json_path, e,
        )
        return False
    if not isinstance(data, dict):
        return False

    # 兼容旧结构：补齐默认字段
    defaults = auto_disable._default_state()
    for k, v in defaults.items():
        data.setdefault(k, v)
    if not isinstance(data.get("pending_restart"), list):
        data["pending_restart"] = []

    # 新 DB 已存在 → 以新为准，绝不覆盖（可能含更新数据 / 已对账过的 disabled
    # 条目）。仍然把旧 JSON 归档，避免下次启动重复处理。
    if os.path.exists(db_path):
        try:
            os.replace(json_path, json_path + ".migrated")
            auto_disable.log.info(
                "auto_node_disable: legacy JSON %s archived in favor of "
                "existing SQLite %s",
                json_path, db_path,
            )
        except Exception as e:
            auto_disable.log.warning(
                "auto_node_disable: failed to archive legacy JSON %s: %s",
                json_path, e,
            )
        return False

    try:
        save_state_to_db(db_path, data)
    except Exception as e:
        auto_disable.log.warning(
            "auto_node_disable: failed to write migrated DB %s: %s",
            db_path, e,
        )
        return False

    archived = json_path + ".migrated"
    try:
        os.replace(json_path, archived)
        auto_disable.log.info(
            "auto_node_disable: migrated legacy JSON %s -> %s "
            "(archived as %s)",
            json_path, db_path, archived,
        )
    except Exception as e:
        auto_disable.log.warning(
            "auto_node_disable: failed to archive legacy JSON %s: %s",
            json_path, e,
        )
    return True