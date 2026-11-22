"""SQLite 数据访问层。

快照 manifest 以 JSON 原文存储(snapshots.manifest_json)作为事实来源,
同时把文件与分区关系规范化到 snapshot_files / file_partitions 两张表,
便于直接 SQL 查询文件引用链。
"""
from __future__ import annotations

import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id  TEXT PRIMARY KEY,
    status       TEXT NOT NULL,               -- COMPLETE | IN_PROGRESS
    created_at   TEXT NOT NULL,
    digest       TEXT NOT NULL,               -- manifest 规范 JSON 的 SHA-256
    manifest_json TEXT NOT NULL,
    uploaded_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_files (
    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, path)
);
CREATE TABLE IF NOT EXISTS file_partitions (
    snapshot_id TEXT NOT NULL,
    path        TEXT NOT NULL,
    partition   TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, path, partition)
);
CREATE TABLE IF NOT EXISTS leases (
    lease_id            TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    required_partitions TEXT NOT NULL,        -- JSON array
    version             INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS retention_rules (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    min_snapshots INTEGER NOT NULL,
    protect_days  INTEGER NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    plan_id      TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    status       TEXT NOT NULL,               -- DRAFT | BLOCKED | CONFIRMED | STALE
    fingerprint  TEXT NOT NULL,               -- 计算方案时的登记表指纹
    payload_json TEXT NOT NULL,
    confirmed_at TEXT
);
"""

DEFAULT_RULES = {"min_snapshots": 0, "protect_days": 0}


def init_db(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---------------------------------------------------------------- snapshots

def count_snapshots(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()["n"]


def list_snapshots(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM snapshots ORDER BY created_at, snapshot_id"
    ).fetchall()


def get_snapshot(conn, snapshot_id: str):
    return conn.execute(
        "SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
    ).fetchone()


def upsert_snapshot(conn, manifest: dict, digest: str, uploaded_at: str) -> None:
    sid = manifest["snapshot_id"]
    conn.execute("DELETE FROM file_partitions WHERE snapshot_id = ?", (sid,))
    conn.execute("DELETE FROM snapshot_files WHERE snapshot_id = ?", (sid,))
    conn.execute(
        "INSERT INTO snapshots (snapshot_id, status, created_at, digest, manifest_json, uploaded_at)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(snapshot_id) DO UPDATE SET"
        "   status=excluded.status, created_at=excluded.created_at,"
        "   digest=excluded.digest, manifest_json=excluded.manifest_json,"
        "   uploaded_at=excluded.uploaded_at",
        (sid, manifest["status"], manifest["created_at"], digest,
         json.dumps(manifest, sort_keys=True), uploaded_at),
    )
    for f in manifest["files"]:
        conn.execute(
            "INSERT INTO snapshot_files (snapshot_id, path, size_bytes) VALUES (?, ?, ?)",
            (sid, f["path"], f["size_bytes"]),
        )
        for p in f["partitions"]:
            conn.execute(
                "INSERT INTO file_partitions (snapshot_id, path, partition) VALUES (?, ?, ?)",
                (sid, f["path"], p),
            )


def delete_snapshot(conn, snapshot_id: str) -> bool:
    conn.execute("DELETE FROM file_partitions WHERE snapshot_id = ?", (snapshot_id,))
    conn.execute("DELETE FROM snapshot_files WHERE snapshot_id = ?", (snapshot_id,))
    cur = conn.execute("DELETE FROM snapshots WHERE snapshot_id = ?", (snapshot_id,))
    return cur.rowcount > 0


def snapshot_obj(row: sqlite3.Row) -> dict:
    manifest = json.loads(row["manifest_json"])
    return {
        "snapshot_id": row["snapshot_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "digest": row["digest"],
        "uploaded_at": row["uploaded_at"],
        "files": manifest["files"],
    }


# ------------------------------------------------------------------- leases

def list_leases(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM leases ORDER BY lease_id").fetchall()


def get_lease(conn, lease_id: str):
    return conn.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()


def insert_lease(conn, lease_id: str, name: str, partitions: list[str], now: str) -> None:
    conn.execute(
        "INSERT INTO leases (lease_id, name, required_partitions, version, created_at, updated_at)"
        " VALUES (?, ?, ?, 1, ?, ?)",
        (lease_id, name, json.dumps(partitions), now, now),
    )


def update_lease(conn, lease_id: str, name: str, partitions: list[str], now: str) -> bool:
    cur = conn.execute(
        "UPDATE leases SET name = ?, required_partitions = ?,"
        " version = version + 1, updated_at = ? WHERE lease_id = ?",
        (name, json.dumps(partitions), now, lease_id),
    )
    return cur.rowcount > 0


def delete_lease(conn, lease_id: str) -> bool:
    cur = conn.execute("DELETE FROM leases WHERE lease_id = ?", (lease_id,))
    return cur.rowcount > 0


def lease_obj(row: sqlite3.Row) -> dict:
    return {
        "lease_id": row["lease_id"],
        "name": row["name"],
        "required_partitions": json.loads(row["required_partitions"]),
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# -------------------------------------------------------------------- rules

def get_rules(conn) -> dict:
    row = conn.execute("SELECT * FROM retention_rules WHERE id = 1").fetchone()
    if row is None:
        return dict(DEFAULT_RULES)
    return {"min_snapshots": row["min_snapshots"], "protect_days": row["protect_days"]}


def upsert_rules(conn, min_snapshots: int, protect_days: int, now: str) -> None:
    conn.execute(
        "INSERT INTO retention_rules (id, min_snapshots, protect_days, updated_at)"
        " VALUES (1, ?, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET"
        "   min_snapshots=excluded.min_snapshots,"
        "   protect_days=excluded.protect_days, updated_at=excluded.updated_at",
        (min_snapshots, protect_days, now),
    )


# -------------------------------------------------------------------- plans

def insert_plan(conn, plan_id: str, created_at: str, status: str,
                fingerprint: str, payload: dict) -> None:
    conn.execute(
        "INSERT INTO plans (plan_id, created_at, status, fingerprint, payload_json)"
        " VALUES (?, ?, ?, ?, ?)",
        (plan_id, created_at, status, fingerprint, json.dumps(payload)),
    )


def get_plan(conn, plan_id: str):
    row = conn.execute("SELECT * FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
    return plan_obj(row) if row else None


def list_plans(conn, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM plans ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
    ).fetchall()
    return [plan_obj(r) for r in rows]


def set_plan_status(conn, plan_id: str, status: str, confirmed_at: str | None = None) -> None:
    conn.execute(
        "UPDATE plans SET status = ?, confirmed_at = COALESCE(?, confirmed_at)"
        " WHERE plan_id = ?",
        (status, confirmed_at, plan_id),
    )


def plan_obj(row: sqlite3.Row) -> dict:
    return {
        "plan_id": row["plan_id"],
        "created_at": row["created_at"],
        "status": row["status"],
        "fingerprint": row["fingerprint"],
        "payload": json.loads(row["payload_json"]),
        "confirmed_at": row["confirmed_at"],
    }
