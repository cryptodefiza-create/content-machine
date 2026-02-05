"""Phase 1 Tests: Core Infrastructure"""
import json
import pytest
import os

SKIP_API_TESTS = not os.getenv("GEMINI_API_KEY")
SKIP_NEWS_TESTS = not os.getenv("NEWS_API_KEY")


class TestUtils:

    def test_generate_content_hash(self):
        from src.utils import generate_content_hash

        hash1 = generate_content_hash("test content")
        hash2 = generate_content_hash("test content")
        hash3 = generate_content_hash("different content")

        assert hash1 == hash2
        assert hash1 != hash3
        assert len(hash1) == 12

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

    def test_scanner_init(self):
        from src.scanner import Scanner

        scanner = Scanner()
        assert scanner.max_retries == 3
        assert scanner.delays["coingecko"] >= 1.0

    def test_trending_coins(self):
        from src.scanner import Scanner

        scanner = Scanner()
        coins = scanner.get_trending_coins(limit=2)

        assert isinstance(coins, list)
        if not coins:
            pytest.skip("CoinGecko returned empty (likely rate-limited)")
        assert "topic" in coins[0]
        assert "source" in coins[0]
        assert coins[0]["type"] == "trend"

    def test_rss_feeds(self):
        from src.scanner import Scanner

        scanner = Scanner()
        articles = scanner.get_rss_feeds(limit=3)

        assert isinstance(articles, list)
        if not articles:
            pytest.skip("All RSS feeds returned empty")
        assert "topic" in articles[0]
        assert "source" in articles[0]
        assert articles[0]["type"] == "news"

    @pytest.mark.skipif(SKIP_NEWS_TESTS, reason="NEWS_API_KEY not set")
    def test_news_articles(self):
        from src.scanner import Scanner

        scanner = Scanner()
        articles = scanner.get_news_articles(limit=2)

        assert isinstance(articles, list)
        if not articles:
            pytest.skip("NewsAPI returned empty results")
        assert "topic" in articles[0]
        assert "url" in articles[0]

    def test_scan_all(self):
        from src.scanner import Scanner

        scanner = Scanner()
        items = scanner.scan_all(max_items=5)

        assert isinstance(items, list)
        assert len(items) <= 5

        if not items:
            pytest.skip("All sources returned empty (likely rate-limited)")
        for item in items:
            assert "content_hash" in item
            assert "topic" in item
            assert "scanned_at" in item

    def test_deduplicate(self):
        from src.scanner import deduplicate

        items = [
            {"topic": "A", "value": 1},
            {"topic": "B", "value": 2},
            {"topic": "A", "value": 3},
        ]

        unique = deduplicate(items, key="topic")
        assert len(unique) == 2
        assert unique[0]["value"] == 1


class TestQueue:

    def test_queue_init(self, queue_manager):
        assert queue_manager.engine is not None
        assert queue_manager.Session is not None

    def test_ping(self, queue_manager):
        assert queue_manager.ping() is True

    def test_add_content(self, queue_manager, sample_content):
        item = queue_manager.add_content(sample_content)

        assert item.id is not None
        assert item.content_hash == sample_content["content_hash"]
        assert item.status == "pending"
        assert item.pro_content == sample_content["pro_post"]["content"]

    def test_content_exists(self, queue_manager, sample_content):
        assert queue_manager.content_exists(sample_content["content_hash"]) is False
        queue_manager.add_content(sample_content)
        assert queue_manager.content_exists(sample_content["content_hash"]) is True

    def test_get_pending(self, queue_manager, sample_content):
        queue_manager.add_content(sample_content)
        pending = queue_manager.get_pending(limit=10)

        assert len(pending) >= 1
        assert pending[0].status == "pending"

    def test_update_status(self, queue_manager, sample_content):
        item = queue_manager.add_content(sample_content)

        result = queue_manager.update_status(item.id, "approved")
        assert result is True

        updated = queue_manager.get_by_id(item.id)
        assert updated.status == "approved"
        assert updated.approved_at is not None

    def test_update_status_nonexistent(self, queue_manager):
        result = queue_manager.update_status(99999, "approved")
        assert result is False

    def test_get_stats(self, queue_manager, sample_content):
        queue_manager.add_content(sample_content)
        stats = queue_manager.get_stats()

        assert "total" in stats
        assert "pending" in stats
        assert "approved" in stats
        assert stats["total"] >= 1

    def test_expire_old_pending(self, queue_manager, sample_content):
        item = queue_manager.add_content(sample_content)

        expired_count = queue_manager.expire_old_pending(hours=0)
        assert expired_count >= 1, "Item created at cutoff time should be expired"

        updated = queue_manager.get_by_id(item.id)
        assert updated.status == "expired"


class TestImagePromptGenerator:

    def test_generate_prompt(self):
        from src.imagen import ImagePromptGenerator

        gen = ImagePromptGenerator()
        prompt = gen.generate_prompt("Blockchain network visualization", "pro")

        assert prompt.persona == "pro"
        assert "16:9" in prompt.copy_paste_prompt
        assert "no text" in prompt.copy_paste_prompt.lower()
        assert prompt.base_prompt == "Blockchain network visualization"

    def test_generate_all_prompts_from_dict(self, sample_content):
        from src.imagen import ImagePromptGenerator

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

    def test_copy_paste_prompt_format(self):
        from src.imagen import ImagePromptGenerator

        gen = ImagePromptGenerator()
        prompt = gen.generate_prompt("Blockchain visualization", "pro")

        assert "," in prompt.copy_paste_prompt
        assert "no text" in prompt.copy_paste_prompt
        assert "Blockchain visualization" in prompt.copy_paste_prompt


@pytest.mark.skipif(SKIP_API_TESTS, reason="GEMINI_API_KEY not set")
class TestBrain:

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

        assert "content" in content["pro_post"]
        assert "content" in content["work_post"]
        assert "content" in content["degen_post"]

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
        from src.brain import Brain, MAX_POST_LENGTH

        brain = Brain()
        content = brain.generate_content(sample_topic)

        if content:
            for persona in ("pro_post", "work_post", "degen_post"):
                length = len(content[persona]["content"])
                assert length <= MAX_POST_LENGTH, (
                    f"{persona} is {length} chars (max {MAX_POST_LENGTH})"
                )


class TestBrainOffline:

    def _make_brain(self):
        """Create a Brain-like object with just the methods we need, no API key required."""
        from src.brain import Brain
        brain = object.__new__(Brain)
        brain.system_prompt = "test"
        return brain

    def _valid_response(self, **overrides):
        data = {
            "pro_post": {"content": "Pro content here", "is_thread": False, "thread_parts": [], "suggested_hashtags": []},
            "work_post": {"content": "Work content here", "is_thread": False, "thread_parts": [], "cashtags": []},
            "degen_post": {"content": "Degen content here", "is_thread": False, "thread_parts": []},
            "visual_prompts": {"pro": "test", "work": "test", "degen": "test"},
        }
        data.update(overrides)
        return json.dumps(data)

    def test_parse_valid_json(self):
        brain = self._make_brain()
        result = brain._parse_response(self._valid_response())
        assert result is not None
        assert "pro_post" in result
        assert result["pro_post"]["content"] == "Pro content here"

    def test_parse_json_with_code_fence(self):
        brain = self._make_brain()
        wrapped = f"```json\n{self._valid_response()}\n```"
        result = brain._parse_response(wrapped)
        assert result is not None
        assert "pro_post" in result

    def test_parse_missing_field(self):
        brain = self._make_brain()
        data = json.dumps({
            "pro_post": {"content": "x"},
            "work_post": {"content": "y"},
            "degen_post": {"content": "z"},
        })
        result = brain._parse_response(data)
        assert result is None

    def test_parse_invalid_json(self):
        brain = self._make_brain()
        result = brain._parse_response("not json at all")
        assert result is None

    def test_parse_persona_missing_content(self):
        brain = self._make_brain()
        data = json.dumps({
            "pro_post": {"content": "ok"},
            "work_post": {"no_content_key": True},
            "degen_post": {"content": "ok"},
            "visual_prompts": {"pro": "t", "work": "t", "degen": "t"},
        })
        result = brain._parse_response(data)
        assert result is None

    def test_parse_visual_prompts_not_dict(self):
        brain = self._make_brain()
        data = json.dumps({
            "pro_post": {"content": "ok"},
            "work_post": {"content": "ok"},
            "degen_post": {"content": "ok"},
            "visual_prompts": "not a dict",
        })
        result = brain._parse_response(data)
        assert result is None

    def test_validate_and_trim_under_limit(self):
        from src.brain import MAX_POST_LENGTH
        brain = self._make_brain()
        content = {
            "pro_post": {"content": "Short post"},
            "work_post": {"content": "Short post"},
            "degen_post": {"content": "Short post"},
        }
        brain._validate_and_trim(content)
        assert content["pro_post"]["content"] == "Short post"

    def test_validate_and_trim_over_limit(self):
        from src.brain import MAX_POST_LENGTH
        brain = self._make_brain()
        long_text = "a" * 300
        content = {
            "pro_post": {"content": long_text},
            "work_post": {"content": "ok"},
            "degen_post": {"content": "ok"},
        }
        brain._validate_and_trim(content)
        assert len(content["pro_post"]["content"]) == MAX_POST_LENGTH
        assert content["pro_post"]["content"].endswith("…")

    def test_validate_and_trim_thread_parts(self):
        from src.brain import MAX_POST_LENGTH
        brain = self._make_brain()
        content = {
            "pro_post": {"content": "ok", "thread_parts": ["a" * 300, "short"]},
            "work_post": {"content": "ok"},
            "degen_post": {"content": "ok"},
        }
        brain._validate_and_trim(content)
        assert len(content["pro_post"]["thread_parts"][0]) == MAX_POST_LENGTH
        assert content["pro_post"]["thread_parts"][1] == "short"


class TestIntegration:

    def test_scanner_to_queue(self, queue_manager):
        from src.scanner import Scanner

        scanner = Scanner()
        items = scanner.scan_all(max_items=2)

        if items:
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
        from src.brain import Brain
        from src.imagen import ImagePromptGenerator

        brain = Brain()
        content = brain.generate_content(sample_topic)

        assert content is not None, "Brain should generate content"

        item = queue_manager.add_content(content)
        assert item.id is not None

        imagen = ImagePromptGenerator()
        prompts = imagen.generate_all_prompts(item)

        assert prompts["pro"] is not None or prompts["work"] is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
