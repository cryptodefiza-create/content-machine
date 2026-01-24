"""Image prompt generator for manual Gemini generation"""
from typing import Dict, Optional, Union
from dataclasses import dataclass

from .utils import logger


@dataclass
class ImagePrompt:
    """Container for image generation prompt"""
    persona: str
    base_prompt: str
    enhanced_prompt: str
    copy_paste_prompt: str  # Single line for easy copying
    style_notes: str


class ImagePromptGenerator:
    """
    Generates copy-paste ready prompts for Gemini image generation.

    Usage:
    1. Get the prompt from Telegram bot or dashboard
    2. Open Gemini (nano banana mode)
    3. Paste the copy_paste_prompt
    4. Generate and download
    """

    STYLES = {
        "pro": {
            "style": "clean professional fintech UI",
            "colors": "deep blue, white, gold accents",
            "elements": "data visualization, infographics, minimal design",
            "mood": "trustworthy, innovative, institutional",
            "avoid": "cartoon, neon, chaos, any text or words or letters",
            "keywords": "Bloomberg aesthetic, corporate, sleek"
        },
        "work": {
            "style": "dark mode trading terminal",
            "colors": "black background, neon green, cyan, red accents",
            "elements": "candlestick charts, data streams, price indicators, order book",
            "mood": "high-signal, urgent, alpha",
            "avoid": "daylight, cartoon, corporate, any text or words or letters",
            "keywords": "TradingView dark theme, Matrix code rain, hacker aesthetic"
        },
        "degen": {
            "style": "cyberpunk glitch art",
            "colors": "neon pink, purple, cyan, vaporwave palette",
            "elements": "glitch effects, pixel corruption, VHS distortion, ASCII art",
            "mood": "rebellious, underground, chaotic energy",
            "avoid": "corporate, clean, realistic, any text or words or letters",
            "keywords": "vaporwave, glitch art, retro futurism, synthwave"
        }
    }

    def generate_prompt(self, base_prompt: str, persona: str) -> ImagePrompt:
        """
        Generate enhanced prompt for a persona.

        Args:
            base_prompt: The raw prompt from Gemini content generation
            persona: One of "pro", "work", "degen"

        Returns:
            ImagePrompt with both detailed and copy-paste versions
        """
        if not base_prompt:
            logger.warning(f"Empty base prompt for {persona}")
            base_prompt = f"Abstract {persona} aesthetic visualization"

        style = self.STYLES.get(persona, self.STYLES["pro"])

        # Detailed prompt with all style info
        enhanced = f"""Subject: {base_prompt}

Visual Style: {style['style']}
Color Palette: {style['colors']}
Key Elements: {style['elements']}
Mood/Feeling: {style['mood']}

MUST AVOID: {style['avoid']}

Technical Requirements:
- Absolutely NO text, words, letters, or numbers in the image
- 16:9 aspect ratio
- High resolution for social media
- Abstract/conceptual interpretation preferred
- Photorealistic or high-quality digital art style"""

        # Single-line prompt optimized for Gemini copy-paste
        copy_paste = self._create_copy_paste_prompt(base_prompt, style)

        return ImagePrompt(
            persona=persona,
            base_prompt=base_prompt,
            enhanced_prompt=enhanced.strip(),
            copy_paste_prompt=copy_paste,
            style_notes=f"Keywords: {style['keywords']}"
        )

    def _create_copy_paste_prompt(self, base_prompt: str, style: dict) -> str:
        """
        Create a single optimized prompt for Gemini.

        Gemini Imagen works best with:
        - Clear subject first
        - Style descriptors
        - Quality keywords
        - Negative prompt at end
        """
        prompt_parts = [
            base_prompt,
            style['style'],
            style['colors'],
            style['elements'],
            "16:9 aspect ratio",
            "high quality digital art",
            "no text no words no letters"
        ]

        return ", ".join(prompt_parts)

    def generate_all_prompts(
        self,
        content_item: Union[object, Dict, None]
    ) -> Dict[str, Optional[ImagePrompt]]:
        """
        Generate prompts for all personas from a content item.

        Args:
            content_item: Either a ContentItem object or dict with
                          {persona}_image_prompt fields

        Returns:
            Dict mapping persona name to ImagePrompt (or None if no prompt)
        """
        if content_item is None:
            logger.warning("content_item is None, returning empty prompts")
            return {"pro": None, "work": None, "degen": None}

        prompts = {}

        for persona in ["pro", "work", "degen"]:
            field_name = f"{persona}_image_prompt"

            # Handle both dict and object access
            if isinstance(content_item, dict):
                base = content_item.get(field_name)
            else:
                base = getattr(content_item, field_name, None)

            if base:
                prompts[persona] = self.generate_prompt(base, persona)
                logger.debug(f"Generated {persona} image prompt")
            else:
                prompts[persona] = None
                logger.debug(f"No base prompt for {persona}")

        return prompts

    def format_for_telegram(self, prompts: Dict[str, Optional[ImagePrompt]]) -> str:
        """
        Format prompts for Telegram message.

        Returns a copy-paste friendly message.
        """
        lines = ["🎨 *Image Prompts*\n"]
        lines.append("Copy these to Gemini (nano banana mode):\n")

        emoji_map = {"pro": "💼", "work": "📊", "degen": "🔥"}

        for persona in ["pro", "work", "degen"]:
            prompt = prompts.get(persona)
            if prompt:
                emoji = emoji_map.get(persona, "🖼")
                lines.append(f"{emoji} *{persona.upper()}*")
                lines.append(f"`{prompt.copy_paste_prompt}`\n")

        return "\n".join(lines)

    def format_for_dashboard(self, prompts: Dict[str, Optional[ImagePrompt]]) -> Dict:
        """Format prompts for web dashboard display"""
        result = {}

        for persona in ["pro", "work", "degen"]:
            prompt = prompts.get(persona)
            if prompt:
                result[persona] = {
                    "base": prompt.base_prompt,
                    "copy_paste": prompt.copy_paste_prompt,
                    "detailed": prompt.enhanced_prompt,
                    "style_notes": prompt.style_notes
                }
            else:
                result[persona] = None

        return result
