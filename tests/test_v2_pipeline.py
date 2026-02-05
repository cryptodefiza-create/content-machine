"""Content Machine v2 tests."""
import json

from src.cache import LLMCache
from src.dedupe import DedupeStore, jaccard_similarity
from src.persona import load_persona_store
from src.pipeline import ContentPipeline
from src.settings import load_settings


class FakeLLM:
    def __init__(self, settings, cache=None, tracker=None):
        self.settings = settings
        self.cache = cache
        self.tracker = tracker

    def generate_json(self, stage: str, persona: str, prompt: str):
        if stage == "SCOUT":
            return {"summary": "Summary", "key_points": ["A"], "risky_claims": [], "safe_claims": []}
        if stage == "IDEATE":
            return {"angles": ["Angle"], "hooks": ["Hook?"], "ctas": ["What do you think?"]}
        if stage == "STYLE_TRANSFER":
            return {"style_notes": "concise, punchy", "patterns": ["short sentences"], "do_not_copy": []}
        if stage == "HOT_TAKE":
            return {"hot_takes": ["Hot take"], "hook_options": ["Hot hook?"], "cta_options": ["Agree?"]}
        if stage in ("DRAFT", "REWRITE"):
            return {
                "content": f"{persona} draft with a hook?",
                "is_thread": False,
                "thread_parts": [],
                "visual_prompt": "Abstract visualization",
            }
        if stage == "QUALITY_CHECK":
            return {"score": 8, "issues": [], "improvements": []}
        raise ValueError(stage)


def test_persona_loader():
    store = load_persona_store("config/personas_v2.json")
    assert "pro" in store.keys()
    assert store.get("pro").name


def test_cache_hit(tmp_path):
    cache = LLMCache(path=tmp_path / "cache.db", ttl_seconds=3600, max_entries=10)
    cache.set("key1", {"ok": True})
    assert cache.get("key1") == {"ok": True}
    assert cache.stats.hits == 1


def test_dedupe_similarity(tmp_path):
    store = DedupeStore(path=tmp_path / "dedupe.db")
    store.add("pro", "privacy infra is shipping fast")
    result = store.check("pro", "privacy infra is shipping quickly", threshold=0.3, window_hours=24)
    assert result.is_duplicate is True


def test_pipeline_stage_wiring(tmp_path):
    settings = load_settings()
    settings["cache"]["enabled"] = False
    settings["dedupe"]["enabled"] = False
    pipeline = ContentPipeline(settings=settings, llm_client_factory=FakeLLM)
    result = pipeline.run({"topic": "Test topic", "type": "manual", "source": "test"}, personas=["pro"], dry_run=True)
    assert result.content_pack is not None
    assert result.per_persona["pro"].stage_history == ["SCOUT", "IDEATE", "STYLE_TRANSFER", "HOT_TAKE", "DRAFT", "QUALITY_CHECK"]
