"""Gemini content generation with retry logic and validation"""
import json
import time
from typing import Dict, Optional

try:  # Optional import to allow offline tests
    from google import genai
    from google.genai import types
except Exception:  # pragma: no cover
    genai = None
    types = None

from .utils import get_env, load_personas, generate_content_hash, logger

MAX_POST_LENGTH = 280
RECOMMENDED_LENGTH = 250


class Brain:

    def __init__(self):
        if genai is None or types is None:
            raise RuntimeError("google-genai is required for Brain")
        self.client = genai.Client(api_key=get_env("GEMINI_API_KEY"))
        self.system_prompt = self._load_system_prompt()
        self.model = get_env("GEMINI_MODEL", "gemini-2.0-flash-lite")
        self.min_delay_between_calls = 5.0
        self.last_call_time = 0
        self.max_retries = 3
        self.retry_backoff = 15.0

    def _load_system_prompt(self) -> str:
        try:
            return load_personas()
        except FileNotFoundError:
            logger.error("personas.md not found - using minimal prompt")
            return "Generate social media content as JSON with pro_post, work_post, degen_post, and visual_prompts fields."

    def _rate_limit(self):
        elapsed = time.time() - self.last_call_time
        if elapsed < self.min_delay_between_calls:
            time.sleep(self.min_delay_between_calls - elapsed)
        self.last_call_time = time.time()

    def _call_gemini(self, prompt: str, temperature: float = 0.8) -> Optional[str]:
        self._rate_limit()

        last_error = None
        for attempt in range(self.max_retries):
            try:
                response = self.client.models.generate_content(
                    model=self.model,
                    config=types.GenerateContentConfig(
                        system_instruction=self.system_prompt,
                        response_mime_type="application/json",
                        temperature=temperature,
                        max_output_tokens=2000
                    ),
                    contents=prompt
                )
                return response.text

            except Exception as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    wait_time = self.retry_backoff * (attempt + 1)
                    logger.warning(f"Gemini call failed (attempt {attempt + 1}): {e}. Retrying in {wait_time}s")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Gemini call failed after {self.max_retries} attempts: {e}")

        return None

    def generate_content(self, topic_data: Dict) -> Optional[Dict]:
        prompt = f"""
INPUT TYPE: {topic_data.get('type', 'trend').upper()}
SOURCE: {topic_data.get('source', 'unknown')}
TOPIC: {topic_data.get('topic', '')}
CONTEXT: {json.dumps(topic_data.get('details', {}))}
URL: {topic_data.get('url', '')}

Generate the 3-persona content pack. Ensure each persona sounds DISTINCTLY different.
Remember: Each post must be under {RECOMMENDED_LENGTH} characters.
"""

        response_text = self._call_gemini(prompt, temperature=0.8)
        if not response_text:
            return None

        content = self._parse_response(response_text)

        if content:
            content["content_hash"] = topic_data.get(
                "content_hash",
                generate_content_hash(topic_data["topic"])
            )
            content["source_topic"] = topic_data["topic"]
            content["source_url"] = topic_data.get("url")
            content["content_type"] = topic_data.get("type", "trend")
            self._validate_and_trim(content)

        return content

    def generate_qt_content(self, kol_post: Dict) -> Optional[Dict]:
        prompt = f"""
INPUT TYPE: KOL Post for Quote Tweet

KOL: @{kol_post.get('username', 'unknown')}
CONTENT: {kol_post.get('content', '')}
URL: {kol_post.get('url', '')}

Generate QT content that ADDS VALUE - don't just agree or restate.
Each persona should have a unique angle on this.
Remember: Each post must be under {RECOMMENDED_LENGTH} characters.
"""

        response_text = self._call_gemini(prompt, temperature=0.85)
        if not response_text:
            return None

        content = self._parse_response(response_text)

        if content:
            content["content_hash"] = generate_content_hash(
                kol_post.get("url", "") + kol_post.get("content", "")
            )
            content["source_topic"] = f"QT @{kol_post.get('username')}: {kol_post.get('content', '')[:80]}"
            content["source_url"] = kol_post.get("url")
            content["content_type"] = "kol_qt"
            self._validate_and_trim(content)

        return content

    def _parse_response(self, text: str) -> Optional[Dict]:
        try:
            text = text.strip()
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

            data = json.loads(text)

            required = ["pro_post", "work_post", "degen_post", "visual_prompts"]
            missing = [k for k in required if k not in data]
            if missing:
                logger.warning(f"Missing required fields: {missing}")
                return None

            for persona in ["pro_post", "work_post", "degen_post"]:
                if not isinstance(data.get(persona), dict) or "content" not in data[persona]:
                    logger.warning(f"{persona} invalid structure")
                    return None

            if not isinstance(data.get("visual_prompts"), dict):
                logger.warning("visual_prompts is not a dict")
                return None

            return data

        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error: {e}")
            logger.debug(f"Raw response: {text[:500]}")
            return None

    def _validate_and_trim(self, content: Dict):
        for key, name in [("pro_post", "PRO"), ("work_post", "WORK"), ("degen_post", "DEGEN")]:
            post = content.get(key, {})
            post_content = post.get("content", "")

            if len(post_content) > MAX_POST_LENGTH:
                logger.warning(f"{name} post was {len(post_content)} chars, truncated to {MAX_POST_LENGTH}")
                post["content"] = post_content[:MAX_POST_LENGTH - 1] + "…"

            for i, part in enumerate(post.get("thread_parts", [])):
                if len(part) > MAX_POST_LENGTH:
                    logger.warning(f"{name} thread part {i+1} was {len(part)} chars, truncated")
                    post["thread_parts"][i] = part[:MAX_POST_LENGTH - 1] + "…"
