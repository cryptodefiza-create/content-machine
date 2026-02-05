"""Settings loader (single source of truth)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from .utils import get_project_root, logger

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    yaml = None


DEFAULT_SETTINGS: Dict[str, Any] = {
    "version": 2,
    "personas_path": "config/personas_v2.json",
    "llm": {
        "provider": "gemini",
        "model": "gemini-2.0-flash-lite",
        "temperature": 0.8,
        "max_output_tokens": 900,
    },
    "pipeline": {
        "stages": ["SCOUT", "IDEATE", "STYLE_TRANSFER", "HOT_TAKE", "DRAFT", "QUALITY_CHECK", "QUEUE"],
        "quality_min_score": 7.0,
        "max_revision_passes": 1,
    },
    "cache": {
        "enabled": True,
        "ttl_seconds": 60 * 60 * 24 * 7,
        "max_entries": 5000,
    },
    "dedupe": {
        "enabled": True,
        "threshold": 0.82,
        "window_hours": 24,
    },
    "rate_limit": {
        "min_delay_seconds": 5.0,
        "max_retries": 3,
        "backoff_seconds": 12.0,
    },
    "costs": {
        "prompt_per_1k_tokens": 0.15,
        "completion_per_1k_tokens": 0.60,
        "currency": "USD",
    },
    "runtime": {
        "dry_run": False,
    },
    "exports": {
        "enabled": True,
        "format": "csv",
        "export_dir": "data/exports",
        "master_csv": True,
        "master_csv_path": "data/exports/all_runs.csv",
    },
}


def _merge_dicts(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


def _load_settings_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        logger.warning(f"Settings file not found at {path}, using defaults")
        return {}

    text = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        if yaml is None:
            raise RuntimeError("PyYAML is required to load YAML settings")
        return yaml.safe_load(text) or {}

    return json.loads(text)


def load_settings() -> Dict[str, Any]:
    root = get_project_root()
    path = root / "config" / "settings.json"
    settings = _merge_dicts(DEFAULT_SETTINGS, _load_settings_file(path))

    env_model = os.getenv("GEMINI_MODEL")
    if env_model:
        settings["llm"]["model"] = env_model

    dry_run_env = os.getenv("DRY_RUN")
    if dry_run_env is not None:
        settings["runtime"]["dry_run"] = dry_run_env.strip().lower() in ("1", "true", "yes", "on")

    return settings
