"""
The shared evidence board.

Every tool result is stored once with an ID (E1, E2, ...), the agent and tool that
produced it, a human-readable summary, a source link and an "as of" time. Agents
see summaries by ID, the final answer cites [E#], and the fact-checker matches the
answer's numbers against the board's text.
"""

import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


@dataclass
class Evidence:
    id: str
    agent: str
    tool: str
    summary: str
    label: str
    url: str | None = None
    kind: str = "other"           # market | news | document | alert
    as_of: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def to_dict(self) -> dict:
        return asdict(self)


class EvidenceBoard:
    def __init__(self):
        self._items: list[Evidence] = []
        self._lock = threading.Lock()

    def add(self, agent: str, tool: str, summary: str, label: str, url: str | None = None,
            kind: str = "other") -> Evidence:
        with self._lock:
            ev = Evidence(f"E{len(self._items) + 1}", agent, tool, summary.strip(), label, url, kind)
            self._items.append(ev)
            return ev

    def get(self, evidence_id: str) -> Evidence | None:
        with self._lock:
            return next((e for e in self._items if e.id == evidence_id), None)

    def items(self) -> list[Evidence]:
        with self._lock:
            return list(self._items)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def for_prompt(self, ids: list[str] | None = None, max_chars: int = 900) -> str:
        """Evidence as prompt lines: '[E3] (market/get_quote, as of …) summary'."""
        chosen = [e for e in self.items() if ids is None or e.id in ids]
        return "\n".join(f"[{e.id}] ({e.agent}/{e.tool}, as of {e.as_of}) {e.summary[:max_chars]}"
                         for e in chosen)

    def text(self) -> str:
        """All evidence text, for number matching."""
        return "\n".join(e.summary for e in self.items())

    def sources(self, cited: set[str] | None = None) -> list[dict]:
        """Sources for the UI, cited ones first."""
        items = self.items()
        if cited:
            items = [e for e in items if e.id in cited] + [e for e in items if e.id not in cited]
        return [{"id": e.id, "kind": e.kind, "label": f"[{e.id}] {e.label}", "url": e.url,
                 "cited": bool(cited and e.id in cited)} for e in items]
