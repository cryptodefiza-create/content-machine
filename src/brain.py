"""Gemini content generation with retry logic and validation"""
import json
import time
from typing import Dict, Optional, List

from google import genai
from google.genai import types

from .utils import get_env, load_personas, generate_content_hash, logger


# X character limits
MAX_POST_LENGTH = 280
MAX_THREAD_PART_LENGTH = 280
RECOMMENDED_LENGTH = 250  # Leave room for engagement


class Brain:
    """
    Gemini-powered content generation engine.

    Generates content for 3 personas from a single topic.
    """

    def __init__(self):
        self.client = genai.Client(api_key=get_env("GEMINI_API_KEY"))
        self.system_prompt = self._load_system_prompt()
        self.model = get_env("GEMINI_MODEL", "gemini-2.0-flash-lite")

        # Rate limiting (respect free tier: 15 req/min)
        self.min_delay_between_calls = 5.0  # seconds - safer for free tier
        self.last_call_time = 0

        # Retry config
        self.max_retries = 3
        self.retry_backoff = 15.0  # longer backoff for rate limits

    def _load_system_prompt(self) -> str:
        """Load personas with error handling"""
        try:
            return load_personas()
        except FileNotFoundError:
            logger.error("personas.md not found - using minimal prompt")
            return "Generate social media content as JSON with pro_post, work_post, degen_post, and visual_prompts fields."
        except Exception as e:
            logger.error(f"Failed to load personas: {e}")
            raise

    def _rate_limit(self):
        """Ensure minimum delay between API calls"""
        elapsed = time.time() - self.last_call_time
        if elapsed < self.min_delay_between_calls:
            time.sleep(self.min_delay_between_calls - elapsed)
        self.last_call_time = time.time()

    def _call_gemini(self, prompt: str, temperature: float = 0.8) -> Optional[str]:
        """Call Gemini API with retry logic"""
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
                    wait_time = self.retry_backoff ** attempt
                    logger.warning(f"Gemini call failed (attempt {attempt + 1}): {e}. Retrying in {wait_time}s")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Gemini call failed after {self.max_retries} attempts: {e}")

        return None

    def generate_content(self, topic_data: Dict) -> Optional[Dict]:
        """Generate 3-persona content pack from topic"""
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
            # Add metadata
            content["content_hash"] = topic_data.get(
                "content_hash",
                generate_content_hash(topic_data["topic"])
            )
            content["source_topic"] = topic_data["topic"]
            content["source_url"] = topic_data.get("url")
            content["content_type"] = topic_data.get("type", "trend")

            # Validate and warn about length issues
            self._validate_lengths(content)

        return content

    def generate_qt_content(self, kol_post: Dict) -> Optional[Dict]:
        """Generate QT content for KOL post"""
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
            # Add metadata
            content["content_hash"] = generate_content_hash(
                kol_post.get("url", "") + kol_post.get("content", "")
            )
            content["source_topic"] = f"QT @{kol_post.get('username')}: {kol_post.get('content', '')[:80]}"
            content["source_url"] = kol_post.get("url")
            content["content_type"] = "kol_qt"

            self._validate_lengths(content)

        return content

    def _parse_response(self, text: str) -> Optional[Dict]:
        """Parse Gemini JSON response with robust handling"""
        if not text:
            return None

        try:
            # Strip markdown code blocks if present (defensive)
            text = text.strip()
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

            # Parse JSON
            data = json.loads(text)

            # Validate required fields
            required = ["pro_post", "work_post", "degen_post", "visual_prompts"]
            missing = [k for k in required if k not in data]
            if missing:
                logger.warning(f"Missing required fields: {missing}")
                return None

            # Validate nested structure
            for persona in ["pro_post", "work_post", "degen_post"]:
                if not isinstance(data.get(persona), dict):
                    logger.warning(f"{persona} is not a dict")
                    return None
                if "content" not in data[persona]:
                    logger.warning(f"{persona} missing 'content' field")
                    return None

            if not isinstance(data.get("visual_prompts"), dict):
                logger.warning("visual_prompts is not a dict")
                return None

            return data

        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error: {e}")
            logger.debug(f"Raw response: {text[:500]}")
            return None

    def _validate_lengths(self, content: Dict) -> List[str]:
        """Check content lengths and log warnings. Returns list of warnings."""
        warnings = []

        personas = [
            ("pro_post", "PRO"),
            ("work_post", "WORK"),
            ("degen_post", "DEGEN")
        ]

        for key, name in personas:
            post = content.get(key, {})
            post_content = post.get("content", "")
            length = len(post_content)

            if length > MAX_POST_LENGTH:
                msg = f"{name} post is {length} chars (max {MAX_POST_LENGTH})"
                warnings.append(msg)
                logger.warning(msg)
            elif length > RECOMMENDED_LENGTH:
                msg = f"{name} post is {length} chars (recommended max {RECOMMENDED_LENGTH})"
                logger.info(msg)

            # Check thread parts if present
            thread_parts = post.get("thread_parts", [])
            for i, part in enumerate(thread_parts):
                part_length = len(part)
                if part_length > MAX_THREAD_PART_LENGTH:
                    msg = f"{name} thread part {i+1} is {part_length} chars (max {MAX_THREAD_PART_LENGTH})"
                    warnings.append(msg)
                    logger.warning(msg)

        return warnings
