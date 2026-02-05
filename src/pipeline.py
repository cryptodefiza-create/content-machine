"""Content Machine v2 pipeline."""
from __future__ import annotations

import json
import uuid
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable

from .cache import LLMCache
from .dedupe import DedupeStore
from .imagen import ImagePromptGenerator
from .llm import LLMClient
from .persona import PersonaProfile, load_persona_store
from .exporter import ExportSettings, export_rows
from .queue import QueueManager
from .runtime_config import get_dry_run
from .settings import load_settings
from .telemetry import RunTracker
from .utils import generate_content_hash, logger

MAX_POST_LENGTH = 280
RECOMMENDED_LENGTH = 250


@dataclass
class TopicData:
    topic: str
    type: str = "trend"
    source: str = "unknown"
    details: dict = field(default_factory=dict)
    url: Optional[str] = None
    content_hash: Optional[str] = None


@dataclass
class DraftResult:
    persona: str
    content: str
    is_thread: bool
    thread_parts: List[str]
    visual_prompt: str
    issues: List[str]
    quality_score: float
    stage_history: List[str]
    angle: str = ""
    hook: str = ""
    cta: str = ""


@dataclass
class PipelineResult:
    run_id: str
    content_pack: Optional[dict]
    per_persona: Dict[str, DraftResult]
    dry_run: bool
    skipped: List[str]


class PipelineTracker:

    def __init__(self, run_id: str):
        self.run_id = run_id
        self._tracker = RunTracker()

    def record(self, usage_record):
        self._tracker.record(usage_record)


class ContentPipeline:

    def __init__(self, settings: Optional[dict] = None, llm_client_factory: Optional[Callable] = None):
        self.settings = settings or load_settings()
        self.persona_store = load_persona_store(self.settings.get("personas_path"))
        self.cache = LLMCache(
            ttl_seconds=self.settings["cache"]["ttl_seconds"],
            max_entries=self.settings["cache"]["max_entries"],
        ) if self.settings["cache"].get("enabled", True) else None
        self.dedupe = DedupeStore() if self.settings["dedupe"].get("enabled", True) else None
        self.queue = QueueManager()
        self.imagen = ImagePromptGenerator()
        self.llm_client_factory = llm_client_factory or LLMClient
        export_cfg = self.settings.get("exports", {})
        self.export_settings = ExportSettings(
            enabled=bool(export_cfg.get("enabled", True)),
            export_dir=str(export_cfg.get("export_dir", "data/exports")),
            format=str(export_cfg.get("format", "csv")),
            master_csv=bool(export_cfg.get("master_csv", True)),
            master_csv_path=str(export_cfg.get("master_csv_path", "data/exports/all_runs.csv")),
        )

    def run(self, topic_data: dict, personas: Optional[List[str]] = None, dry_run: Optional[bool] = None) -> PipelineResult:
        run_id = uuid.uuid4().hex[:12]
        tracker = PipelineTracker(run_id)
        llm = self.llm_client_factory(self.settings, cache=self.cache, tracker=tracker)

        personas = personas or self.persona_store.keys()
        if dry_run is None:
            dry_run = get_dry_run(default_dry_run=self.settings["runtime"].get("dry_run", False))

        per_persona: Dict[str, DraftResult] = {}
        skipped: List[str] = []
        topic = self._normalize_topic(topic_data)

        for persona_key in personas:
            persona = self.persona_store.get(persona_key)
            try:
                draft = self._run_for_persona(llm, topic, persona)
            except Exception as e:
                logger.error(f"Run {run_id}: persona {persona_key} failed: {e}")
                skipped.append(persona_key)
                continue

            if draft is None:
                skipped.append(persona_key)
            else:
                per_persona[persona_key] = draft

        if not per_persona:
            return PipelineResult(run_id=run_id, content_pack=None, per_persona={}, dry_run=dry_run, skipped=skipped)

        content_pack = self._build_content_pack(topic, per_persona, run_id)
        try:
            export_status = "dry_run" if dry_run else "pending"
            export_rows(self.export_settings, run_id, topic.__dict__, per_persona, status=export_status)
        except Exception as e:
            logger.error(f"Run {run_id}: export failed: {e}")
        if not dry_run:
            self.queue.add_content(content_pack)
            logger.info(f"Run {run_id}: queued draft for {list(per_persona.keys())}")
        else:
            logger.info(f"Run {run_id}: dry run, not queued")

        return PipelineResult(run_id=run_id, content_pack=content_pack, per_persona=per_persona, dry_run=dry_run, skipped=skipped)

    def _normalize_topic(self, topic_data: dict) -> TopicData:
        content_hash = topic_data.get("content_hash") or generate_content_hash(topic_data.get("topic", ""))
        return TopicData(
            topic=topic_data.get("topic", ""),
            type=topic_data.get("type", "trend"),
            source=topic_data.get("source", "unknown"),
            details=topic_data.get("details", {}) or {},
            url=topic_data.get("url"),
            content_hash=content_hash,
        )

    def _run_for_persona(self, llm: LLMClient, topic: TopicData, persona: PersonaProfile) -> Optional[DraftResult]:
        stage_history: List[str] = []

        scout = self._stage_scout(llm, topic, persona)
        stage_history.append("SCOUT")

        ideate = self._stage_ideate(llm, topic, persona, scout)
        stage_history.append("IDEATE")

        style = self._stage_style_transfer(llm, topic, persona, scout, ideate)
        stage_history.append("STYLE_TRANSFER")

        hot_take = self._stage_hot_take(llm, topic, persona, scout, ideate)
        stage_history.append("HOT_TAKE")

        draft = self._stage_draft(llm, topic, persona, scout, ideate, style, hot_take)
        stage_history.append("DRAFT")

        quality = self._stage_quality(llm, topic, persona, draft)
        stage_history.append("QUALITY_CHECK")

        min_score = self.settings["pipeline"].get("quality_min_score", 7.0)
        max_passes = self.settings["pipeline"].get("max_revision_passes", 1)
        passes = 0
        while (quality["score"] < min_score or quality["issues"]) and passes < max_passes:
            draft = self._stage_rewrite(llm, topic, persona, draft, quality)
            quality = self._stage_quality(llm, topic, persona, draft)
            passes += 1

        content = draft["content"].strip()
        if "#" in content:
            quality["issues"].append("Contains hashtags (forbidden)")

        if len(content) > MAX_POST_LENGTH:
            content = content[:MAX_POST_LENGTH - 1] + "…"
            quality["issues"].append("Trimmed over length limit")

        if self.dedupe:
            dedupe_cfg = self.settings["dedupe"]
            result = self.dedupe.check(
                persona.key,
                content,
                threshold=dedupe_cfg.get("threshold", 0.82),
                window_hours=dedupe_cfg.get("window_hours", 24),
            )
            if result.is_duplicate:
                quality["issues"].append("Duplicate similarity detected")
                draft = self._stage_rewrite(llm, topic, persona, draft, quality, avoid_text=result.matched_text)
                content = draft["content"].strip()
                result2 = self.dedupe.check(
                    persona.key,
                    content,
                    threshold=dedupe_cfg.get("threshold", 0.82),
                    window_hours=dedupe_cfg.get("window_hours", 24),
                )
                if result2.is_duplicate:
                    return None

            self.dedupe.add(persona.key, content)

        return DraftResult(
            persona=persona.key,
            content=content,
            is_thread=bool(draft.get("is_thread")),
            thread_parts=draft.get("thread_parts", []),
            visual_prompt=draft.get("visual_prompt") or topic.topic,
            issues=quality["issues"],
            quality_score=float(quality["score"]),
            stage_history=stage_history,
            angle=(ideate.get("angles") or [""])[0],
            hook=(ideate.get("hooks") or [""])[0],
            cta=(ideate.get("ctas") or [""])[0],
        )

    def _stage_scout(self, llm: LLMClient, topic: TopicData, persona: PersonaProfile) -> dict:
        prompt = (
            "You are a research scout. Summarize the topic and extract only safe claims.\n"
            "Return JSON with keys: summary (string), key_points (list), risky_claims (list), safe_claims (list).\n\n"
            f"TOPIC TYPE: {topic.type}\n"
            f"SOURCE: {topic.source}\n"
            f"TOPIC: {topic.topic}\n"
            f"DETAILS: {json.dumps(topic.details)}\n"
            f"URL: {topic.url or ''}\n"
        )
        return llm.generate_json("SCOUT", persona.key, prompt)

    def _stage_ideate(self, llm: LLMClient, topic: TopicData, persona: PersonaProfile, scout: dict) -> dict:
        prompt = (
            "You are an ideation assistant. Propose strong angles, hooks, and CTAs.\n"
            "Return JSON with keys: angles (list), hooks (list), ctas (list).\n\n"
            f"PERSONA: {persona.name}\n"
            f"BIO: {persona.bio}\n"
            f"ROLE: {persona.role}\n"
            f"TONE: meme={persona.tone.meme}, serious={persona.tone.serious}, educational={persona.tone.educational}\n"
            f"STANCE: {persona.stance}\n"
            f"HOT TAKES: {persona.hot_takes}\n"
            f"FORBIDDEN: {persona.forbidden_phrases}\n"
            f"EXAMPLES: {persona.examples}\n\n"
            f"SCOUT SUMMARY: {scout.get('summary', '')}\n"
            f"KEY POINTS: {scout.get('key_points', [])}\n"
        )
        return llm.generate_json("IDEATE", persona.key, prompt)

    def _stage_draft(self, llm: LLMClient, topic: TopicData, persona: PersonaProfile, scout: dict, ideate: dict, style: dict, hot_take: dict) -> dict:
        prompt = (
            "You are drafting a single X/Twitter post. No hashtags. Max 250 chars preferred, hard max 280.\n"
            "Return JSON with keys: content (string), is_thread (bool), thread_parts (list), visual_prompt (string).\n\n"
            f"PERSONA: {persona.name}\n"
            f"VOICE BIO: {persona.bio}\n"
            f"ROLE: {persona.role}\n"
            f"TONE SLIDERS: meme={persona.tone.meme}, serious={persona.tone.serious}, educational={persona.tone.educational}\n"
            f"STANCE BULLETS: {persona.stance}\n"
            f"HOT TAKES: {persona.hot_takes}\n"
            f"FORBIDDEN PHRASES: {persona.forbidden_phrases}\n"
            f"EXAMPLES: {persona.examples}\n\n"
            f"TOPIC: {topic.topic}\n"
            f"DETAILS: {json.dumps(topic.details)}\n"
            f"SCOUT SUMMARY: {scout.get('summary', '')}\n"
            f"ANGLES: {ideate.get('angles', [])}\n"
            f"HOOKS: {ideate.get('hooks', [])}\n"
            f"CTAS: {ideate.get('ctas', [])}\n"
            f"STYLE NOTES: {style.get('style_notes', '')}\n"
            f"STYLE PATTERNS: {style.get('patterns', [])}\n"
            f"DO NOT COPY: {style.get('do_not_copy', [])}\n"
            f"HOT TAKE OPTIONS: {hot_take.get('hot_takes', [])}\n"
            f"HOT HOOKS: {hot_take.get('hook_options', [])}\n"
            f"HOT CTAS: {hot_take.get('cta_options', [])}\n"
        )
        return llm.generate_json("DRAFT", persona.key, prompt)

    def _stage_style_transfer(self, llm: LLMClient, topic: TopicData, persona: PersonaProfile, scout: dict, ideate: dict) -> dict:
        example = ""
        if isinstance(topic.details, dict):
            example = topic.details.get("style_example", "") or topic.details.get("reference_post", "")

        prompt = (
            "You are a style analyst. Extract voice patterns and structure without copying.\n"
            "Return JSON with keys: style_notes (string), patterns (list), do_not_copy (list).\n\n"
            f"PERSONA: {persona.name}\n"
            f"PERSONA EXAMPLES: {persona.examples}\n"
            f"TOPIC: {topic.topic}\n"
            f"SCOUT SUMMARY: {scout.get('summary', '')}\n"
            f"STYLE EXAMPLE: {example}\n"
        )
        return llm.generate_json("STYLE_TRANSFER", persona.key, prompt)

    def _stage_hot_take(self, llm: LLMClient, topic: TopicData, persona: PersonaProfile, scout: dict, ideate: dict) -> dict:
        prompt = (
            "Generate spicy but safe hot-take options. No financial advice.\n"
            "Return JSON with keys: hot_takes (list), hook_options (list), cta_options (list).\n\n"
            f"PERSONA: {persona.name}\n"
            f"STANCE: {persona.stance}\n"
            f"HOT TAKES: {persona.hot_takes}\n"
            f"TOPIC: {topic.topic}\n"
            f"SCOUT SUMMARY: {scout.get('summary', '')}\n"
        )
        return llm.generate_json("HOT_TAKE", persona.key, prompt)

    def _stage_quality(self, llm: LLMClient, topic: TopicData, persona: PersonaProfile, draft: dict) -> dict:
        issues = self._heuristic_issues(draft.get("content", ""), persona)
        prompt = (
            "You are a strict editor. Score the draft 0-10 and list issues.\n"
            "Catch bland hooks, repetition, vague claims, weak CTAs.\n"
            "Return JSON with keys: score (number), issues (list), improvements (list).\n\n"
            f"PERSONA: {persona.name}\n"
            f"FORBIDDEN PHRASES: {persona.forbidden_phrases}\n"
            f"DRAFT: {draft.get('content', '')}\n"
        )
        result = llm.generate_json("QUALITY_CHECK", persona.key, prompt)
        combined = list(set(issues + result.get("issues", [])))
        result["issues"] = combined
        return result

    def _stage_rewrite(self, llm: LLMClient, topic: TopicData, persona: PersonaProfile, draft: dict, quality: dict, avoid_text: Optional[str] = None) -> dict:
        prompt = (
            "Rewrite the draft to address issues. No hashtags. Max 250 chars preferred, hard max 280.\n"
            "Return JSON with keys: content (string), is_thread (bool), thread_parts (list), visual_prompt (string).\n\n"
            f"PERSONA: {persona.name}\n"
            f"ISSUES: {quality.get('issues', [])}\n"
            f"IMPROVEMENTS: {quality.get('improvements', [])}\n"
            f"AVOID TEXT: {avoid_text or ''}\n"
            f"ORIGINAL: {draft.get('content', '')}\n"
        )
        return llm.generate_json("REWRITE", persona.key, prompt)

    def _heuristic_issues(self, content: str, persona: PersonaProfile) -> List[str]:
        issues: List[str] = []
        lower = content.lower()
        bland_hooks = ["interesting", "thoughts", "just", "maybe", "could be"]
        if any(b in lower[:80] for b in bland_hooks):
            issues.append("Bland hook")
        if lower.count("?") == 0:
            issues.append("Weak CTA")
        vague_terms = ["something", "things", "various", "some", "many"]
        if any(v in lower for v in vague_terms):
            issues.append("Vague claim")
        tokens = re.findall(r"[a-zA-Z0-9']+", lower)
        bigrams = {}
        for i in range(len(tokens) - 1):
            bg = f"{tokens[i]} {tokens[i+1]}"
            bigrams[bg] = bigrams.get(bg, 0) + 1
        if any(count >= 2 for count in bigrams.values()):
            issues.append("Repetition")
        for banned in persona.forbidden_phrases:
            if banned.lower() in lower:
                issues.append(f"Forbidden phrase: {banned}")
        return issues

    def _build_content_pack(self, topic: TopicData, per_persona: Dict[str, DraftResult], run_id: str) -> dict:
        def persona_entry(key: str) -> dict:
            draft = per_persona.get(key)
            if not draft:
                return {"content": "", "is_thread": False, "thread_parts": [], "suggested_hashtags": []}
            return {
                "content": draft.content,
                "is_thread": draft.is_thread,
                "thread_parts": draft.thread_parts,
                "suggested_hashtags": [],
            }

        return {
            "content_hash": topic.content_hash,
            "content_type": topic.type,
            "source_topic": topic.topic,
            "source_url": topic.url,
            "topic_summary": topic.details.get("description", "") if topic.details else topic.topic[:120],
            "run_id": run_id,
            "pipeline_version": "v2",
            "quality_score": sum([d.quality_score for d in per_persona.values()]) / max(len(per_persona), 1),
            "pro_post": persona_entry("pro"),
            "work_post": persona_entry("work"),
            "degen_post": persona_entry("degen"),
            "visual_prompts": {
                "pro": per_persona.get("pro").visual_prompt if per_persona.get("pro") else None,
                "work": per_persona.get("work").visual_prompt if per_persona.get("work") else None,
                "degen": per_persona.get("degen").visual_prompt if per_persona.get("degen") else None,
            },
            "engagement_notes": "Generated by Content Machine v2 pipeline",
        }
