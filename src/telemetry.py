"""Run tracking + cost estimation."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Optional

from .utils import get_project_root, logger


@dataclass
class UsageRecord:
    run_id: str
    persona: str
    stage: str
    prompt_tokens: int
    completion_tokens: int
    cost: float
    cached: bool
    timestamp: int


class RunTracker:

    def __init__(self, path: Optional[Path] = None):
        if path is None:
            path = get_project_root() / "data" / "run_logs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path

    def record(self, record: UsageRecord):
        with open(self.path, "a") as f:
            f.write(json.dumps(asdict(record)) + "\n")

    def summarize(self, run_id: str) -> Dict[str, float]:
        totals = {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
        if not self.path.exists():
            return totals

        with open(self.path, "r") as f:
            for line in f:
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("run_id") != run_id:
                    continue
                totals["prompt_tokens"] += int(data.get("prompt_tokens", 0))
                totals["completion_tokens"] += int(data.get("completion_tokens", 0))
                totals["cost"] += float(data.get("cost", 0.0))
        return totals


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, int(len(text) / 4))


def estimate_cost(prompt_tokens: int, completion_tokens: int, rates: Dict[str, float]) -> float:
    prompt_rate = rates.get("prompt_per_1k_tokens", 0.0)
    completion_rate = rates.get("completion_per_1k_tokens", 0.0)
    cost = (prompt_tokens / 1000.0) * prompt_rate + (completion_tokens / 1000.0) * completion_rate
    return round(cost, 6)


def now_ts() -> int:
    return int(time.time())
