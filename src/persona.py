"""Persona config loader + validation."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Any

from pydantic import BaseModel, Field, ValidationError

from .utils import get_project_root, logger

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None


class ToneSliders(BaseModel):
    meme: float = Field(ge=0.0, le=1.0)
    serious: float = Field(ge=0.0, le=1.0)
    educational: float = Field(ge=0.0, le=1.0)


class PersonaProfile(BaseModel):
    key: str
    name: str
    handle: str
    bio: str
    role: str
    tone: ToneSliders
    forbidden_phrases: List[str] = []
    stance: List[str] = []
    hot_takes: List[str] = []
    examples: List[str] = []


class PersonaConfig(BaseModel):
    version: int
    personas: Dict[str, PersonaProfile]


@dataclass
class PersonaStore:
    config: PersonaConfig

    def get(self, key: str) -> PersonaProfile:
        if key not in self.config.personas:
            raise KeyError(f"Persona '{key}' not found")
        return self.config.personas[key]

    def keys(self) -> List[str]:
        return list(self.config.personas.keys())


def _load_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Persona config not found: {path}")

    raw = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        if yaml is None:
            raise RuntimeError("PyYAML is required to load YAML personas")
        return yaml.safe_load(raw) or {}

    return json.loads(raw)


def load_persona_store(path: str | None = None) -> PersonaStore:
    if path is None:
        path = "config/personas_v2.yaml"
    full_path = get_project_root() / path

    data = _load_file(full_path)
    try:
        config = PersonaConfig.model_validate(data)
    except ValidationError as e:
        logger.error(f"Persona config validation error: {e}")
        raise

    return PersonaStore(config=config)
