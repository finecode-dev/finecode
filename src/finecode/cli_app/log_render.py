# docs: docs/wm-protocol.md
"""Pure rendering of ``server/logRecords`` notification params into CLI text lines.

No I/O — kept separate from run_cmd.py so it is trivially unit-testable.
"""

from __future__ import annotations


def render_log_records(params: dict) -> list[str]:
    """Render a ``server/logRecords`` notification's params into display lines."""
    lines = [
        f"[{r.get('level', '')}] {r.get('source', '')} {r.get('group', '')}: {r.get('message', '')}"
        for r in (params or {}).get("records", [])
    ]
    dropped = (params or {}).get("droppedCount")
    if dropped:
        lines.append(f"... dropped {dropped} log records")
    return lines


__all__ = ["render_log_records"]
