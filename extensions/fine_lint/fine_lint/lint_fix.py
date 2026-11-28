from __future__ import annotations

import dataclasses
import enum

from finecode_extension_api import common_types
from finecode_extension_api.resource_uri import ResourceUri

# Re-export for convenience so callers can import Range/Position from here
Position = common_types.Position
Range = common_types.Range


@dataclasses.dataclass
class TextEdit:
    range: Range
    new_text: str


class FixApplicability(enum.StrEnum):
    SAFE = "safe"
    """Automated application is safe; no semantic risk."""
    UNSAFE = "unsafe"
    """Applying may change behavior; user should review."""
    DISPLAY_ONLY = "display_only"
    """Informational only; not automatically applicable."""


@dataclasses.dataclass
class LintFix:
    fix_id: str
    """Handler-generated identifier. Unique within a single GetLintFixesRunResult and
    MUST be deterministic for identical file content: resolve re-runs the handler and
    matches on fix_id to recover the fix, so an id that depends on anything other than
    the fix's own content and position (e.g. its position in an iteration order) breaks
    re-resolution as soon as an unrelated fix is added or removed earlier in the file.
    Used by the WM LSP layer as the codeAction/resolve key when fixes are returned as
    stubs.

    Recommended shape: ``{tool}:{code}:{line}:{character}:{occurrence}``, where
    ``occurrence`` disambiguates multiple fixes at the same ``{tool}:{code}:{line}:
    {character}`` key (e.g. ruff 'remove unused import' vs. 'add noqa') by counting
    occurrences of that key rather than by a global index."""

    title: str
    """User-facing label, e.g. 'Remove unused import `os`'."""

    kind: str
    """LSP code-action kind: 'quickfix', 'source.fixAll.ruff', 'source.organizeImports', ..."""

    edits: dict[ResourceUri, list[TextEdit]]
    """File → text edits. Multi-file from day one to accommodate cross-file fixes.
    Empty dict is valid (e.g. a display-only fix with title but no edits)."""

    target_range: Range
    """Range of the diagnostic this fix addresses. Used for filtering — callers find
    fixes for a diagnostic by matching (target_range, target_codes)."""

    target_codes: list[str]
    """Codes of the diagnostic(s) this fix addresses. Empty for pure source actions
    (organize imports, fix all) that are not tied to a specific diagnostic."""

    is_preferred: bool = False
    """LSP 'preferred' flag — highlighted by default in the IDE menu."""

    applicability: FixApplicability = FixApplicability.SAFE
    """Whether the fix is safe to auto-apply."""
