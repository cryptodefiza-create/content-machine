"""Shared utilities"""
import os
import json
import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='{"timestamp": "%(asctime)s", "level": "%(levelname)s", "module": "%(module)s", "message": "%(message)s"}',
    datefmt='%Y-%m-%dT%H:%M:%S'
)
logger = logging.getLogger("content-machine")


class _RedactFilter(logging.Filter):
    _token_pattern = re.compile(r"bot\\d+:[A-Za-z0-9_-]+")

    def __init__(self, token: str = ""):
        super().__init__()
        self._token = token
        self._extra_tokens = []
        if os.getenv("GEMINI_API_KEY"):
            self._extra_tokens.append(os.getenv("GEMINI_API_KEY", ""))

    def filter(self, record: logging.LogRecord) -> bool:
        msg = str(record.getMessage())
        msg = self._token_pattern.sub("bot<redacted>", msg)
        if self._token:
            msg = msg.replace(self._token, "<redacted>")
        for t in self._extra_tokens:
            if t:
                msg = msg.replace(t, "<redacted>")
        record.msg = msg
        record.args = ()
        return True

# Optional file logging
_log_file = os.getenv("LOG_FILE")
if _log_file:
    try:
        Path(_log_file).parent.mkdir(parents=True, exist_ok=True)
        _handler = logging.FileHandler(_log_file)
        _handler.setFormatter(logging.Formatter(
            '{"timestamp": "%(asctime)s", "level": "%(levelname)s", "module": "%(module)s", "message": "%(message)s"}',
            datefmt='%Y-%m-%dT%H:%M:%S'
        ))
        if not any(isinstance(h, logging.FileHandler) for h in logger.handlers):
            logger.addHandler(_handler)
    except Exception:
        pass

# Apply redaction filter to root + app logger
_redact = _RedactFilter(os.getenv("TELEGRAM_BOT_TOKEN", ""))
logging.getLogger().addFilter(_redact)
logger.addFilter(_redact)


def get_env(key: str, default: Optional[str] = None) -> str:
    value = os.getenv(key, default)
    if value is None and default is None:
        raise ValueError(f"Missing required environment variable: {key}")
    return value


def get_project_root() -> Path:
    current = Path(__file__).resolve().parent

    while current != current.parent:
        if (current / "config").is_dir():
            return current
        current = current.parent

    fallback = Path(__file__).resolve().parent.parent
    logger.warning(f"Could not find config/ directory, using fallback: {fallback}")
    return fallback


def load_config(filename: str) -> dict:
    config_path = get_project_root() / "config" / filename

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    try:
        with open(config_path, "r") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {filename}: {e}")


def load_personas() -> str:
    personas_path = get_project_root() / "config" / "personas.md"

    if not personas_path.exists():
        raise FileNotFoundError(f"Personas file not found: {personas_path}")

    with open(personas_path, "r") as f:
        return f.read()


def generate_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:12]


def truncate(text: str, max_length: int = 100) -> str:
    if not text:
        return ""
    if len(text) <= max_length:
        return text
    return text[:max_length - 3] + "..."
