"""Shared utilities"""
import os
import json
import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='{"timestamp": "%(asctime)s", "level": "%(levelname)s", "module": "%(module)s", "message": "%(message)s"}',
    datefmt='%Y-%m-%dT%H:%M:%S'
)
logger = logging.getLogger("content-machine")


def get_env(key: str, default: Optional[str] = None) -> str:
    """Get environment variable with optional default"""
    value = os.getenv(key, default)
    if value is None and default is None:
        raise ValueError(f"Missing required environment variable: {key}")
    return value


def get_project_root() -> Path:
    """Get project root by finding the config directory"""
    current = Path(__file__).resolve().parent

    # Walk up until we find config/ directory
    while current != current.parent:
        if (current / "config").is_dir():
            return current
        current = current.parent

    # Fallback to parent of src/
    fallback = Path(__file__).resolve().parent.parent
    logger.warning(f"Could not find config/ directory, using fallback: {fallback}")
    return fallback


def load_config(filename: str) -> dict:
    """Load JSON config file"""
    config_path = get_project_root() / "config" / filename

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    try:
        with open(config_path, "r") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {filename}: {e}")


def load_personas() -> str:
    """Load personas markdown file"""
    personas_path = get_project_root() / "config" / "personas.md"

    if not personas_path.exists():
        raise FileNotFoundError(f"Personas file not found: {personas_path}")

    with open(personas_path, "r") as f:
        return f.read()


def generate_content_hash(content: str) -> str:
    """Generate hash for deduplication"""
    return hashlib.sha256(content.encode()).hexdigest()[:12]


def truncate(text: str, max_length: int = 100) -> str:
    """Truncate text with ellipsis"""
    if not text:
        return ""
    if len(text) <= max_length:
        return text
    return text[:max_length - 3] + "..."


def now_iso() -> str:
    """Get current UTC timestamp in ISO format"""
    return datetime.utcnow().isoformat() + "Z"


def now_utc() -> datetime:
    """Get current UTC datetime"""
    return datetime.utcnow()
