"""Simple sqlite cache for LLM calls."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .utils import get_project_root, logger


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0


class LLMCache:

    def __init__(self, path: Optional[Path] = None, ttl_seconds: int = 7 * 24 * 3600, max_entries: int = 5000):
        if path is None:
            path = get_project_root() / "data" / "llm_cache.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.stats = CacheStats()
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_cache (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.commit()

    def _is_expired(self, created_at: int) -> bool:
        return int(time.time()) - created_at > self.ttl_seconds

    def get(self, key: str) -> Optional[dict]:
        with sqlite3.connect(self.path) as conn:
            row = conn.execute(
                "SELECT value, created_at FROM llm_cache WHERE key = ?",
                (key,),
            ).fetchone()
            if not row:
                self.stats.misses += 1
                return None

            value, created_at = row
            if self._is_expired(created_at):
                conn.execute("DELETE FROM llm_cache WHERE key = ?", (key,))
                conn.commit()
                self.stats.misses += 1
                return None

            self.stats.hits += 1
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                logger.warning("Cache entry corrupted, deleting")
                conn.execute("DELETE FROM llm_cache WHERE key = ?", (key,))
                conn.commit()
                return None

    def set(self, key: str, value: dict):
        payload = json.dumps(value)
        now = int(time.time())
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_cache (key, value, created_at) VALUES (?, ?, ?)",
                (key, payload, now),
            )
            self._enforce_max_entries(conn)
            conn.commit()

    def _enforce_max_entries(self, conn):
        if self.max_entries <= 0:
            return
        count = conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0]
        if count <= self.max_entries:
            return
        to_delete = count - self.max_entries
        conn.execute(
            "DELETE FROM llm_cache WHERE key IN ("
            "SELECT key FROM llm_cache ORDER BY created_at ASC LIMIT ?)",
            (to_delete,),
        )

    def reset_stats(self):
        self.stats = CacheStats()
