import json
import sqlite3
import threading
import time
from typing import Any, Optional


class CacheManager:
    def __init__(self, db_path: str = "lol_cache.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        # Keep one persistent connection so :memory: works and we avoid
        # reconnect overhead on every operation.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_db()

    def _init_db(self):
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )
            """)
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_expires ON cache(expires_at)")
            self._conn.commit()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM cache WHERE key = ? AND expires_at > ?",
                (key, time.time()),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, key: str, value: Any, ttl: int = 3600):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO cache (key, value, expires_at) VALUES (?, ?, ?)",
                (key, json.dumps(value), time.time() + ttl),
            )
            self._conn.commit()

    def delete(self, key: str):
        with self._lock:
            self._conn.execute("DELETE FROM cache WHERE key = ?", (key,))
            self._conn.commit()

    def cleanup(self) -> int:
        with self._lock:
            deleted = self._conn.execute(
                "DELETE FROM cache WHERE expires_at <= ?", (time.time(),)
            ).rowcount
            self._conn.commit()
        return deleted

    def stats(self) -> dict:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
            valid = self._conn.execute(
                "SELECT COUNT(*) FROM cache WHERE expires_at > ?", (time.time(),)
            ).fetchone()[0]
        return {"total_entries": total, "valid_entries": valid, "expired": total - valid}

    def ensure_user_profiles_table(self):
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS user_profiles (
                    discord_id TEXT PRIMARY KEY,
                    riot_id TEXT NOT NULL
                )
            """)
            self._conn.commit()

    def save_user_profile(self, discord_id: str, riot_id: str):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO user_profiles (discord_id, riot_id) VALUES (?, ?)",
                (discord_id, riot_id),
            )
            self._conn.commit()

    def load_user_profile(self, discord_id: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT riot_id FROM user_profiles WHERE discord_id = ?", (discord_id,)
            ).fetchone()
        return row[0] if row else None

    def close(self):
        self._conn.close()
