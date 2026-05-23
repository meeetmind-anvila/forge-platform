"""
Auth layer: Bearer token management.
Tokens are stored as bcrypt hashes. Never in plaintext.
"""
import hashlib
import os
import secrets
import sqlite3
import sys
from typing import Optional

import bcrypt


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_auth_db(db_path: str) -> None:
    conn = _connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tokens (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL UNIQUE,
            hash       TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()
    conn.close()


def create_token(db_path: str, name: str) -> str:
    """
    Generate a new random token, store its bcrypt hash, return the raw token.
    The raw token is shown once and never stored.
    """
    raw = secrets.token_urlsafe(32)
    hashed = bcrypt.hashpw(raw.encode(), bcrypt.gensalt(rounds=12)).decode()
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO tokens (name, hash) VALUES (?, ?)",
            (name, hashed),
        )
        conn.commit()
    finally:
        conn.close()
    return raw


def get_token_identity(db_path: str, raw_token: str) -> Optional[str]:
    """
    Verify a raw Bearer token against stored hashes.
    Returns the token name (identity) if valid, else None.
    This is O(n) over tokens — fine for small numbers of service accounts.
    """
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT name, hash FROM tokens").fetchall()
    finally:
        conn.close()

    encoded = raw_token.encode()
    for row in rows:
        try:
            if bcrypt.checkpw(encoded, row["hash"].encode()):
                return row["name"]
        except Exception:
            continue
    return None


def list_tokens(db_path: str):
    """List token names (not hashes)."""
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT name, created_at FROM tokens ORDER BY id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def revoke_token(db_path: str, name: str) -> bool:
    conn = _connect(db_path)
    try:
        cur = conn.execute("DELETE FROM tokens WHERE name=?", (name,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] not in {"create", "list", "revoke"}:
        print("Usage:")
        print("  python auth.py create <name> [db_path]")
        print("  python auth.py list [db_path]")
        print("  python auth.py revoke <name> [db_path]")
        return 2

    command = sys.argv[1]
    db_path = os.getenv("FORGE_AUTH_DB", "/data/registry/auth.db")

    if command == "list":
        if len(sys.argv) >= 3:
            db_path = sys.argv[2]
        init_auth_db(db_path)
        for row in list_tokens(db_path):
            print(f"{row['name']}\t{row['created_at']}")
        return 0

    name = sys.argv[2]
    if len(sys.argv) >= 4:
        db_path = sys.argv[3]

    init_auth_db(db_path)
    if command == "create":
        token = create_token(db_path, name)
        print(token)
        return 0

    if command == "revoke":
        if not revoke_token(db_path, name):
            print(f"Token not found: {name}", file=sys.stderr)
            return 1
        print(f"Revoked {name}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
