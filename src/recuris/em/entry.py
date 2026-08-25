"""EM entry — one markdown file with frontmatter, one unit of external memory.

The manual cabinet is a FILE library, never a Python dict (requirement C.2):
adding a tool's exemplar = adding one md file, zero code change. Frontmatter
declares what the entry is and when it is delivered; retrieval/triggering read
only the frontmatter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from recuris.errors import ProfileError

VALID_TYPES = {"knowledge", "procedure", "action_result"}


@dataclass(frozen=True)
class EMEntry:
    id: str
    type: str                       # knowledge | procedure | action_result
    body: str                       # markdown content after frontmatter
    trigger_event: str = ""         # e.g. "pre_write" (see events.Event)
    trigger_tool: str = ""          # tool name, or "*" for generic fallback
    source: str = ""                # provenance: where this entry was earned
    path: str = ""
    extra: dict = field(default_factory=dict)

    @staticmethod
    def parse(path: Path) -> "EMEntry":
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            raise ProfileError(f"{path}: EM entry must start with '---' frontmatter")
        try:
            _, fm_text, body = text.split("---", 2)
        except ValueError as e:
            raise ProfileError(f"{path}: malformed frontmatter") from e
        fm = yaml.safe_load(fm_text) or {}
        etype = str(fm.get("type") or "")
        if etype not in VALID_TYPES:
            raise ProfileError(f"{path}: type must be one of {sorted(VALID_TYPES)}")
        trigger = fm.get("trigger") or {}
        known = {"id", "type", "trigger", "source"}
        return EMEntry(
            id=str(fm.get("id") or path.stem),
            type=etype,
            body=body.strip("\n"),
            trigger_event=str(trigger.get("event") or ""),
            trigger_tool=str(trigger.get("tool") or ""),
            source=str(fm.get("source") or ""),
            path=str(path),
            extra={k: v for k, v in fm.items() if k not in known},
        )
