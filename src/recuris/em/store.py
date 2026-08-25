"""EM store — loads a profile's em/ directory and answers delivery queries."""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from recuris.em.entry import EMEntry


class EMStore:
    def __init__(self, entries: list[EMEntry]):
        self.entries = entries

    @staticmethod
    def load(em_dir: str | Path) -> "EMStore":
        em_dir = Path(em_dir)
        entries: list[EMEntry] = []
        if em_dir.exists():
            for p in sorted(em_dir.rglob("*.md")):
                if p.name.lower() == "readme.md":
                    continue  # docs-about-the-library, not a library entry
                entries.append(EMEntry.parse(p))
        else:
            logger.warning(f"[em] directory missing: {em_dir} (empty store)")
        return EMStore(entries)

    def query(
        self, *, event: str = "", tool: str = "", type: str = ""
    ) -> list[EMEntry]:
        """Exact-match query; '*'-tool entries only match when asked for '*'."""
        out = []
        for e in self.entries:
            if event and e.trigger_event != event:
                continue
            if tool and e.trigger_tool != tool:
                continue
            if type and e.type != type:
                continue
            out.append(e)
        return out

    def for_tool(self, event: str, tool: str) -> EMEntry | None:
        """Entry for a specific tool at an event, falling back to the '*' generic."""
        hits = self.query(event=event, tool=tool)
        if hits:
            return hits[0]
        generic = self.query(event=event, tool="*")
        return generic[0] if generic else None

    def all_for_tool(self, event: str, tool: str) -> list[EMEntry]:
        """Every entry for an event, tool-specific first then the '*' generics.

        ``for_tool`` returns only ``hits[0]``, which silently caps a boundary
        carrier at one card no matter how many the archive holds — the archive
        can grow but the agent never sees past the alphabetically-first file.
        Callers that can present several cards use this instead; the ordering
        keeps the tool-specific entries ahead of the generic ones so a caller
        that truncates still keeps the most specific advice.
        """
        exact = self.query(event=event, tool=tool) if tool != "*" else []
        generic = self.query(event=event, tool="*")
        seen: set[int] = set()
        out: list[EMEntry] = []
        for e in (*exact, *generic):
            if id(e) in seen:
                continue
            seen.add(id(e))
            out.append(e)
        return out
