"""Image prompt generator for Gemini"""
from typing import Dict, Optional
from dataclasses import dataclass

PERSONAS = ("pro", "work", "degen")


@dataclass
class ImagePrompt:
    persona: str
    base_prompt: str
    copy_paste_prompt: str


class ImagePromptGenerator:

    STYLES = {
        "pro": {
            "style": "clean privacy-tech data visualization",
            "colors": "deep blue, teal, white, gold accents",
            "elements": "privacy shields, encrypted data streams, macro charts, ZK motifs, subtle glow effects",
            "avoid": "cartoon, neon, chaos, any text or words or letters",
        },
        "work": {
            "style": "dark mode trading terminal",
            "colors": "black background, neon green, cyan, red accents",
            "elements": "candlestick charts, data streams, price indicators, order book",
            "avoid": "daylight, cartoon, corporate, any text or words or letters",
        },
        "degen": {
            "style": "cyberpunk glitch art",
            "colors": "neon pink, purple, cyan, vaporwave palette",
            "elements": "glitch effects, pixel corruption, VHS distortion, ASCII art",
            "avoid": "corporate, clean, realistic, any text or words or letters",
        }
    }

    def generate_prompt(self, base_prompt: str, persona: str) -> ImagePrompt:
        if not base_prompt:
            base_prompt = f"Abstract {persona} aesthetic visualization"

        style = self.STYLES.get(persona, self.STYLES["pro"])

        copy_paste = ", ".join([
            base_prompt,
            style["style"],
            style["colors"],
            style["elements"],
            "16:9 aspect ratio",
            "high quality digital art",
            "no text no words no letters"
        ])

        return ImagePrompt(
            persona=persona,
            base_prompt=base_prompt,
            copy_paste_prompt=copy_paste,
        )

    def generate_all_prompts(self, content_item) -> Dict[str, Optional[ImagePrompt]]:
        if content_item is None:
            return {p: None for p in PERSONAS}

        prompts = {}
        for persona in PERSONAS:
            field_name = f"{persona}_image_prompt"

            if isinstance(content_item, dict):
                base = content_item.get(field_name)
            else:
                base = getattr(content_item, field_name, None)

            prompts[persona] = self.generate_prompt(base, persona) if base else None

        return prompts
