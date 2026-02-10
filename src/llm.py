"""LLM client wrapper with caching, rate limiting, and cost tracking."""
from __future__ import annotations

import json
import time
import hashlib
from typing import Dict, Optional

from .utils import get_env, logger
from .cache import LLMCache
from .telemetry import estimate_tokens, estimate_cost, UsageRecord, now_ts


class LLMClient:

    def __init__(self, settings: Dict, cache: Optional[LLMCache] = None, tracker=None):
        self.settings = settings
        self.model = settings["llm"].get("model")
        self.temperature = settings["llm"].get("temperature", 0.8)
        self.max_output_tokens = settings["llm"].get("max_output_tokens", 900)
        self.cache = cache
        self.tracker = tracker
        self.min_delay = settings["rate_limit"].get("min_delay_seconds", 5.0)
        self.max_retries = settings["rate_limit"].get("max_retries", 3)
        self.backoff = settings["rate_limit"].get("backoff_seconds", 10.0)
        self.last_call = 0.0
        self._client = None
        self._types = None

    def _init_client(self):
        if self._client is not None:
            return
        try:
            from google import genai
            from google.genai import types
        except Exception as e:
            raise RuntimeError("google-genai is required for LLM calls") from e

        self._client = genai.Client(api_key=get_env("GEMINI_API_KEY"))
        self._types = types

    def _rate_limit(self):
        elapsed = time.time() - self.last_call
        if elapsed < self.min_delay:
            time.sleep(self.min_delay - elapsed)
        self.last_call = time.time()

    def _cache_key(self, stage: str, persona: str, prompt: str) -> str:
        payload = json.dumps({
            "stage": stage,
            "persona": persona,
            "model": self.model,
            "prompt": prompt,
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def generate_json(self, stage: str, persona: str, prompt: str) -> Dict:
        cache_key = self._cache_key(stage, persona, prompt)
        if self.cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                logger.info(f"LLM cache hit: {stage}/{persona}")
                self._record_usage(stage, persona, prompt, json.dumps(cached), cached=True)
                return cached

        self._init_client()
        self._rate_limit()

        last_error = None
        for attempt in range(self.max_retries):
            try:
                response = self._client.models.generate_content(
                    model=self.model,
                    config=self._types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=self.temperature,
                        max_output_tokens=self.max_output_tokens,
                    ),
                    contents=prompt,
                )
                text = response.text or ""
                data = self._parse_json(text)
                if self.cache:
                    self.cache.set(cache_key, data)
                self._record_usage(stage, persona, prompt, text, cached=False)
                return data
            except Exception as e:
                last_error = e
                wait_time = self.backoff * (attempt + 1)
                logger.warning(f"LLM {stage} attempt {attempt + 1} failed: {e}. Retrying in {wait_time}s")
                time.sleep(wait_time)

        raise RuntimeError(f"LLM call failed after {self.max_retries} attempts: {last_error}")

    @staticmethod
    def _parse_json(text: str) -> Dict:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
        cleaned = cleaned.strip()
        data = json.loads(cleaned)
        if isinstance(data, list):
            data = data[0] if len(data) == 1 and isinstance(data[0], dict) else {"items": data}
        return data

    def _record_usage(self, stage: str, persona: str, prompt: str, completion: str, cached: bool):
        if self.tracker is None:
            return
        prompt_tokens = estimate_tokens(prompt)
        completion_tokens = estimate_tokens(completion)
        cost = estimate_cost(prompt_tokens, completion_tokens, self.settings.get("costs", {}))
        record = UsageRecord(
            run_id=self.tracker.run_id,
            persona=persona,
            stage=stage,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=cost,
            cached=cached,
            timestamp=now_ts(),
        )
        self.tracker.record(record)
