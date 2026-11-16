"""Outlets (brands) the bot produces for. Each one is a folder brands/<key>/ with a brand.json.

Everything outlet-specific lives there: name, hashtag, topics and importance filter, scenario style, cover style,
video templates, cover icon, subtitle highlight colour, voice, bulletin sections. A file path that is missing or
empty in brand.json falls back to the shared default, so a new outlet can start from a few files.
Adding an outlet = adding a folder; no code change.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).parent
DEFAULT_FILES = {
    "topics": "prompts/scout_topics.md",
    "filter": "prompts/news_filter.md",
    "writer_style": "prompts/writer_style.md",
    "cover_style": "prompts/cover_style.md",
    "bulletin_sections": "prompts/bulletin_sections.txt",
    "bulletin_style": "prompts/bulletin_style.md",
    "template_9x16": "assets/brand/template_9x16.png",
    "template_4x5": "assets/brand/template_4x5.png",
    "cover_icon": "assets/brand/logo_icon_circle.jpg",
}


@dataclass
class Brand:
    key: str
    name: str                      # Persian name shown to users and used in prompts
    emoji: str = "📰"
    hashtag: str = ""              # always the first hashtag of a caption
    outlet_en: str = ""            # one English sentence describing the outlet, for the AI prompts
    voice_id: str = ""             # ElevenLabs voice; empty = ELEVENLABS_VOICE_ID from .env
    highlight: str = "#FBB816"     # subtitle colour of the word being spoken
    order: int = 0
    files: dict = field(default_factory=dict)

    def path(self, name: str) -> Path:
        rel = self.files.get(name) or DEFAULT_FILES[name]
        return ROOT / rel

    def text(self, name: str) -> str:
        return self.path(name).read_text(encoding="utf-8")

    def lines(self, name: str) -> list[str]:
        """Non-empty lines of a text file, without '#' comment lines."""
        return [l.strip() for l in self.text(name).splitlines() if l.strip() and not l.strip().startswith("#")]

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.name}"

    def ass_highlight(self) -> str:
        """#RRGGBB -> ASS &H00BBGGRR&"""
        h = self.highlight.lstrip("#")
        return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}&".upper()


def load_all() -> dict[str, Brand]:
    out = []
    for f in sorted((ROOT / "brands").glob("*/brand.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        data.setdefault("key", f.parent.name)
        out.append(Brand(**data))
    out.sort(key=lambda b: (b.order, b.key))
    return {b.key: b for b in out}


BRANDS = load_all()
DEFAULT = next(iter(BRANDS))


def get(key: str | None) -> Brand:
    return BRANDS.get(key or DEFAULT) or BRANDS[DEFAULT]
