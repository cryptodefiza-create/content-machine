"""Pytest fixtures"""
import os
import sys
import pytest
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

os.environ.setdefault("ENVIRONMENT", "test")


@pytest.fixture
def test_db_url(tmp_path):
    db_path = tmp_path / "test_content.db"
    return f"sqlite:///{db_path}"


@pytest.fixture
def queue_manager(test_db_url):
    from src.queue import QueueManager
    return QueueManager(db_url=test_db_url)


@pytest.fixture
def sample_topic():
    from src.utils import generate_content_hash
    return {
        "type": "news",
        "source": "Test",
        "topic": "Ethereum announces major privacy upgrade with zero-knowledge proofs",
        "details": {"description": "A test description for the privacy upgrade"},
        "url": "https://example.com/test-article",
        "content_hash": generate_content_hash("Ethereum announces major privacy upgrade")
    }


@pytest.fixture
def sample_content(sample_topic):
    return {
        "content_hash": sample_topic["content_hash"],
        "content_type": sample_topic["type"],
        "source_topic": sample_topic["topic"],
        "source_url": sample_topic["url"],
        "topic_summary": "Ethereum privacy upgrade announcement",
        "pro_post": {
            "content": "The shift toward privacy-preserving infrastructure represents a significant milestone.",
            "is_thread": False,
            "thread_parts": [],
            "suggested_hashtags": ["#Ethereum", "#Privacy"]
        },
        "work_post": {
            "content": "$ETH privacy upgrade incoming. Most are sleeping on this.",
            "is_thread": False,
            "thread_parts": [],
            "cashtags": ["$ETH"]
        },
        "degen_post": {
            "content": "eth going full privacy mode. ngmi if you're not paying attention anon",
            "is_thread": False,
            "thread_parts": []
        },
        "visual_prompts": {
            "pro": "Abstract blockchain network with privacy shields",
            "work": "Dark trading terminal with ETH chart",
            "degen": "Glitchy ethereum logo with matrix code"
        },
        "engagement_notes": "Post during US market hours"
    }


@pytest.fixture
def sample_kol_post():
    return {
        "username": "vitalikbuterin",
        "content": "Privacy is normal. Privacy is not suspicious.",
        "url": "https://x.com/vitalikbuterin/status/123456789"
    }
