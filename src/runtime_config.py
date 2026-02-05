"""Runtime toggles (dry-run)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Dict

from .utils import get_project_root, logger


_RUNTIME_LOCK = Lock()


@dataclass
class RuntimeConfig:
    dry_run: bool = False


def _path() -> Path:
    path = get_project_root() / "data" / "runtime.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def load_runtime_config(default_dry_run: bool = False) -> RuntimeConfig:
    path = _path()
    if not path.exists():
        return RuntimeConfig(dry_run=default_dry_run)
    try:
        data = json.loads(path.read_text())
        return RuntimeConfig(dry_run=bool(data.get("dry_run", default_dry_run)))
    except Exception as e:
        logger.warning(f"Failed to load runtime config: {e}")
        return RuntimeConfig(dry_run=default_dry_run)


def set_dry_run(enabled: bool):
    with _RUNTIME_LOCK:
        path = _path()
        payload = {"dry_run": bool(enabled)}
        path.write_text(json.dumps(payload))


def get_dry_run(default_dry_run: bool = False) -> bool:
    return load_runtime_config(default_dry_run=default_dry_run).dry_run
