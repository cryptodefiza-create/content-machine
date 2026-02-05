"""Draft deduplication using n-gram Jaccard similarity."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from .utils import get_project_root, logger


_WORD_RE = re.compile(r"[a-zA-Z0-9']+")


def _normalize(text: str) -> List[str]:
    tokens = _WORD_RE.findall(text.lower())
    return [t for t in tokens if len(t) > 1]


def _ngrams(tokens: List[str], n: int = 3) -> set:
    if len(tokens) < n:
        return set(tokens)
    return {" ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}


def jaccard_similarity(a: str, b: str, n: int = 3) -> float:
    ta = _ngrams(_normalize(a), n)
    tb = _ngrams(_normalize(b), n)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass
class DedupeResult:
    is_duplicate: bool
    similarity: float
    matched_text: Optional[str] = None


class DedupeStore:

    def __init__(self, path: Optional[Path] = None):
        if path is None:
            path = get_project_root() / "data" / "dedupe.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dedupe_drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    persona TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def add(self, persona: str, content: str):
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT INTO dedupe_drafts (persona, content, created_at) VALUES (?, ?, ?)",
                (persona, content, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()

    def fetch_recent(self, persona: str, window_hours: int) -> List[str]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute(
                "SELECT content FROM dedupe_drafts WHERE persona = ? AND created_at >= ?",
                (persona, cutoff.isoformat()),
            ).fetchall()
        return [row[0] for row in rows]

    def check(self, persona: str, content: str, threshold: float, window_hours: int) -> DedupeResult:
        best_sim = 0.0
        best_text = None
        for existing in self.fetch_recent(persona, window_hours):
            sim = jaccard_similarity(content, existing, n=3)
            if sim > best_sim:
                best_sim = sim
                best_text = existing
        return DedupeResult(is_duplicate=best_sim >= threshold, similarity=best_sim, matched_text=best_text)
