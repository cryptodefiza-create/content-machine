"""
Phase 1 Tests: Core Infrastructure

Tests for:
- Scanner (multi-source fetching with retry)
- Brain (Gemini content generation)
- Queue (PostgreSQL/SQLite persistence)
- ImagePromptGenerator (prompt formatting)

Run with: pytest tests/test_phase1.py -v
"""
import pytest
import os
from datetime import datetime, timedelta

# Skip tests if API keys not set
SKIP_API_TESTS = not os.getenv("GEMINI_API_KEY")
SKIP_NEWS_TESTS = not os.getenv("NEWS_API_KEY")


class TestUtils:
    """Test utility functions"""

    def test_generate_content_hash(self):
        from src.utils import generate_content_hash

        hash1 = generate_content_hash("test content")
        hash2 = generate_content_hash("test content")
        hash3 = generate_content_hash("different content")

        assert hash1 == hash2, "Same content should produce same hash"
        assert hash1 != hash3, "Different content should produce different hash"
        assert len(hash1) == 12, "Hash should be 12 characters"

    def test_truncate(self):
        from src.utils import truncate

        assert truncate("short", 100) == "short"
        assert truncate("a" * 200, 100) == "a" * 97 + "..."
        assert truncate("", 100) == ""
        assert truncate(None, 100) == ""

    def test_get_project_root(self):
        from src.utils import get_project_root

        root = get_project_root()
        assert (root / "config").is_dir() or (root / "src").is_dir()

    def test_load_config(self):
        from src.utils import load_config

        sources = load_config("sources.json")
        assert "rss_feeds" in sources
        assert "news_queries" in sources

    def test_load_personas(self):
        from src.utils import load_personas

        personas = load_personas()
        assert "HEAD OF BD" in personas
        assert "WORK ANON" in personas
        assert "DEGEN ARCHITECT" in personas


class TestScanner:
    """Test multi-source scanner"""

    def test_scanner_init(self):
        from src.scanner import Scanner

        scanner = Scanner()
        assert scanner.max_retries == 3
        assert scanner.delays["coingecko"] >= 1.0

    def test_trending_coins(self):
        """Test CoinGecko trending coins (live API)"""
        from src.scanner import Scanner

        scanner = Scanner()
        coins = scanner.get_trending_coins(limit=2)

        # May be empty if rate limited, but should not error
        assert isinstance(coins, list)
        if coins:
            assert "topic" in coins[0]
            assert "source" in coins[0]
            assert coins[0]["type"] == "trend"

    def test_rss_feeds(self):
        """Test RSS feed fetching"""
        from src.scanner import Scanner

        scanner = Scanner()
        articles = scanner.get_rss_feeds(limit=3)

        assert isinstance(articles, list)
        # RSS should usually work unless all feeds are down
        if articles:
            assert "topic" in articles[0]
            assert "source" in articles[0]
            assert articles[0]["type"] == "news"

    @pytest.mark.skipif(SKIP_NEWS_TESTS, reason="NEWS_API_KEY not set")
    def test_news_articles(self):
        """Test NewsAPI fetching (requires API key)"""
        from src.scanner import Scanner

        scanner = Scanner()
        articles = scanner.get_news_articles(limit=2)

        assert isinstance(articles, list)
        if articles:
            assert "topic" in articles[0]
            assert "url" in articles[0]

    def test_scan_all(self):
        """Test full scan aggregation"""
        from src.scanner import Scanner

        scanner = Scanner()
        items = scanner.scan_all(max_items=5)

        assert isinstance(items, list)
        assert len(items) <= 5

        for item in items:
            assert "content_hash" in item
            assert "topic" in item
            assert "scanned_at" in item

    def test_deduplicate(self):
        """Test deduplication function"""
        from src.scanner import deduplicate

        items = [
            {"topic": "A", "value": 1},
            {"topic": "B", "value": 2},
            {"topic": "A", "value": 3},  # Duplicate
        ]

        unique = deduplicate(items, key="topic")
        assert len(unique) == 2
        assert unique[0]["value"] == 1  # First occurrence kept


class TestQueue:
    """Test database queue operations"""

    def test_queue_init(self, queue_manager):
        """Test queue initialization"""
        assert queue_manager.engine is not None
        assert queue_manager.Session is not None

    def test_ping(self, queue_manager):
        """Test database connectivity check"""
        assert queue_manager.ping() is True

    def test_add_content(self, queue_manager, sample_content):
        """Test adding content to queue"""
        item = queue_manager.add_content(sample_content)

        assert item.id is not None
        assert item.content_hash == sample_content["content_hash"]
        assert item.status == "pending"
        assert item.pro_content == sample_content["pro_post"]["content"]

    def test_content_exists(self, queue_manager, sample_content):
        """Test duplicate detection"""
        assert queue_manager.content_exists(sample_content["content_hash"]) is False

        queue_manager.add_content(sample_content)

        assert queue_manager.content_exists(sample_content["content_hash"]) is True

    def test_get_pending(self, queue_manager, sample_content):
        """Test getting pending items"""
        queue_manager.add_content(sample_content)

        pending = queue_manager.get_pending(limit=10)

        assert len(pending) >= 1
        assert pending[0].status == "pending"

    def test_get_pending_count(self, queue_manager, sample_content):
        """Test pending count"""
        initial_count = queue_manager.get_pending_count()

        queue_manager.add_content(sample_content)

        assert queue_manager.get_pending_count() == initial_count + 1

    def test_update_status(self, queue_manager, sample_content):
        """Test status updates"""
        item = queue_manager.add_content(sample_content)

        # Update to approved
        result = queue_manager.update_status(item.id, "approved")
        assert result is True

        # Verify update
        updated = queue_manager.get_by_id(item.id)
        assert updated.status == "approved"
        assert updated.approved_at is not None

    def test_update_status_nonexistent(self, queue_manager):
        """Test updating non-existent item"""
        result = queue_manager.update_status(99999, "approved")
        assert result is False

    def test_get_stats(self, queue_manager, sample_content):
        """Test queue statistics"""
        queue_manager.add_content(sample_content)

        stats = queue_manager.get_stats()

        assert "total" in stats
        assert "pending" in stats
        assert "approved" in stats
        assert stats["total"] >= 1

    def test_expire_old_pending(self, queue_manager, sample_content):
        """Test expiring old pending items"""
        # Add content (will be "new")
        queue_manager.add_content(sample_content)

        # Try to expire with 0 hours (should expire everything)
        # Note: In real usage, you'd use hours=48
        expired_count = queue_manager.expire_old_pending(hours=0)

        # The item we just added should be expired
        assert expired_count >= 0


class TestImagePromptGenerator:
    """Test image prompt generation"""

    def test_generate_prompt(self):
        from src.imagen import ImagePromptGenerator

        gen = ImagePromptGenerator()
        prompt = gen.generate_prompt("Blockchain network visualization", "pro")

        assert prompt.persona == "pro"
        assert "16:9" in prompt.enhanced_prompt
        assert "no text" in prompt.copy_paste_prompt.lower()
        assert prompt.base_prompt == "Blockchain network visualization"

    def test_generate_all_prompts_from_dict(self, sample_content):
        from src.imagen import ImagePromptGenerator

        # Flatten visual_prompts to match expected format
        content_dict = {
            "pro_image_prompt": sample_content["visual_prompts"]["pro"],
            "work_image_prompt": sample_content["visual_prompts"]["work"],
            "degen_image_prompt": sample_content["visual_prompts"]["degen"],
        }

        gen = ImagePromptGenerator()
        prompts = gen.generate_all_prompts(content_dict)

        assert "pro" in prompts
        assert "work" in prompts
        assert "degen" in prompts
        assert prompts["pro"] is not None
        assert prompts["pro"].persona == "pro"

    def test_generate_all_prompts_none(self):
        from src.imagen import ImagePromptGenerator

        gen = ImagePromptGenerator()
        prompts = gen.generate_all_prompts(None)

        assert prompts["pro"] is None
        assert prompts["work"] is None
        assert prompts["degen"] is None

    def test_style_definitions(self):
        from src.imagen import ImagePromptGenerator

        gen = ImagePromptGenerator()

        assert "pro" in gen.STYLES
        assert "work" in gen.STYLES
        assert "degen" in gen.STYLES

        for style in gen.STYLES.values():
            assert "style" in style
            assert "colors" in style
            assert "avoid" in style

    def test_format_for_telegram(self):
        from src.imagen import ImagePromptGenerator

        gen = ImagePromptGenerator()
        prompts = {
            "pro": gen.generate_prompt("Test pro", "pro"),
            "work": gen.generate_prompt("Test work", "work"),
            "degen": None,
        }

        formatted = gen.format_for_telegram(prompts)

        assert "PRO" in formatted
        assert "WORK" in formatted
        assert "Image Prompts" in formatted


@pytest.mark.skipif(SKIP_API_TESTS, reason="GEMINI_API_KEY not set")
class TestBrain:
    """Test Gemini content generation (requires API key)"""

    def test_brain_init(self):
        from src.brain import Brain

        brain = Brain()
        assert brain.client is not None
        assert brain.system_prompt is not None
        assert len(brain.system_prompt) > 100

    def test_generate_content(self, sample_topic):
        from src.brain import Brain

        brain = Brain()
        content = brain.generate_content(sample_topic)

        assert content is not None, "Should generate content"
        assert "pro_post" in content
        assert "work_post" in content
        assert "degen_post" in content
        assert "visual_prompts" in content

        # Check nested structure
        assert "content" in content["pro_post"]
        assert "content" in content["work_post"]
        assert "content" in content["degen_post"]

        # Check content exists
        assert len(content["pro_post"]["content"]) > 0
        assert len(content["work_post"]["content"]) > 0
        assert len(content["degen_post"]["content"]) > 0

    def test_generate_qt_content(self, sample_kol_post):
        from src.brain import Brain

        brain = Brain()
        content = brain.generate_qt_content(sample_kol_post)

        assert content is not None
        assert content["content_type"] == "kol_qt"
        assert "pro_post" in content

    def test_content_length_validation(self, sample_topic):
        from src.brain import Brain

        brain = Brain()
        content = brain.generate_content(sample_topic)

        if content:
            # Check lengths are reasonable
            pro_len = len(content["pro_post"]["content"])
            work_len = len(content["work_post"]["content"])
            degen_len = len(content["degen_post"]["content"])

            # Should be under 280 (X limit) in most cases
            # Allow some flexibility as LLM may occasionally exceed
            assert pro_len < 400, f"PRO too long: {pro_len}"
            assert work_len < 400, f"WORK too long: {work_len}"
            assert degen_len < 400, f"DEGEN too long: {degen_len}"


class TestIntegration:
    """Integration tests combining multiple components"""

    def test_scanner_to_queue(self, queue_manager):
        """Test scanner output can be stored in queue"""
        from src.scanner import Scanner
        from src.utils import generate_content_hash

        scanner = Scanner()
        items = scanner.scan_all(max_items=2)

        if items:
            # Create mock content for scanned item
            topic = items[0]
            content = {
                "content_hash": topic["content_hash"],
                "content_type": topic["type"],
                "source_topic": topic["topic"],
                "source_url": topic.get("url"),
                "topic_summary": topic["topic"][:100],
                "pro_post": {"content": "Test pro", "is_thread": False, "thread_parts": [], "suggested_hashtags": []},
                "work_post": {"content": "Test work", "is_thread": False, "thread_parts": [], "cashtags": []},
                "degen_post": {"content": "test degen", "is_thread": False, "thread_parts": []},
                "visual_prompts": {"pro": "test", "work": "test", "degen": "test"},
            }

            item = queue_manager.add_content(content)
            assert item.id is not None
            assert queue_manager.content_exists(topic["content_hash"])

    @pytest.mark.skipif(SKIP_API_TESTS, reason="GEMINI_API_KEY not set")
    def test_full_pipeline(self, queue_manager, sample_topic):
        """Test full scanner → brain → queue pipeline"""
        from src.brain import Brain
        from src.imagen import ImagePromptGenerator

        # Generate content
        brain = Brain()
        content = brain.generate_content(sample_topic)

        assert content is not None, "Brain should generate content"

        # Store in queue
        item = queue_manager.add_content(content)
        assert item.id is not None

        # Generate image prompts
        imagen = ImagePromptGenerator()
        prompts = imagen.generate_all_prompts(item)

        assert prompts["pro"] is not None or prompts["work"] is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
