"""
SQLite metadata layer for the artifact registry.
Schema:
  artifacts(name TEXT, version TEXT, sha256 TEXT, size INT,
            publisher TEXT, published_at TEXT, deps TEXT,
            PRIMARY KEY (name, version))
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_metadata_db(db_path: str) -> None:
    conn = _connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS artifacts (
            name         TEXT NOT NULL,
            version      TEXT NOT NULL,
            sha256       TEXT NOT NULL,
            size         INTEGER NOT NULL,
            publisher    TEXT NOT NULL,
            published_at TEXT NOT NULL,
            deps         TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (name, version)
        )
        """
    )
    conn.commit()
    conn.close()


def add_artifact(
    db_path: str,
    name: str,
    version: str,
    sha256: str,
    size: int,
    publisher: str,
    deps: List[Dict[str, str]],
) -> None:
    """
    Insert a new artifact. Raises sqlite3.IntegrityError on duplicate (name, version).
    """
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO artifacts (name, version, sha256, size, publisher, published_at, deps)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                version,
                sha256,
                size,
                publisher,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(deps),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_artifact(db_path: str, name: str, version: str) -> Optional[Dict[str, Any]]:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM artifacts WHERE name=? AND version=?", (name, version)
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["deps"] = json.loads(d["deps"])
        return d
    finally:
        conn.close()


def get_artifact_versions(db_path: str, name: str) -> List[Dict[str, Any]]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT version, sha256, size, published_at FROM artifacts WHERE name=? ORDER BY published_at",
            (name,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_all_versions_for_name(db_path: str, name: str) -> List[Dict[str, Any]]:
    """Return full metadata rows for all versions of a package."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM artifacts WHERE name=?", (name,)
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["deps"] = json.loads(d["deps"])
            result.append(d)
        return result
    finally:
        conn.close()


# Type alias
ArtifactMeta = Dict[str, Any]