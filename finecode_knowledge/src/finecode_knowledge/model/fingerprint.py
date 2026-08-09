"""Input fingerprints: what a bucket was extracted from, checkable later (R11).

``goals.md`` §4.8 requires the dependency graph and input fingerprints to be
**persisted with the facts**, so a cold start with no watcher -- a CLI
invocation, the common case -- can tell which buckets are still current instead
of trusting facts it cannot verify.

Two values per input, doing different jobs (§4.8, Q4):

- ``size`` + ``mtime_ns`` are the **cheap gate**. Unchanged means the file is
  not read at all, which is what keeps every invocation stat-bound.
- ``sha256`` is what actually **decides**. mtime moves on checkout, clone and
  rebase with identical content, so a verdict resting on it alone
  false-invalidates constantly (R8, which is explicit that the hash and not the
  mtime is the input).

**A leaf module**: it imports only ``errors``, so the store can hold
fingerprints and the verification walk can check them without either importing
the other.
"""

from __future__ import annotations

import dataclasses
import hashlib
import pathlib

from finecode_knowledge.model.errors import SchemaError

__all__ = ["InputFingerprint", "capture", "resolve"]

_CHUNK = 1 << 20


@dataclasses.dataclass(frozen=True)
class InputFingerprint:
    """One input file as it stood when the facts were extracted."""

    path: str
    """As the unit declared it: workspace-relative POSIX under the root,
    absolute otherwise (ADR-0023 D3)."""
    size: int
    mtime_ns: int
    sha256: str

    def to_json(self) -> dict:
        return {
            "path": self.path,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "sha256": self.sha256,
        }

    @classmethod
    def from_json(cls, data: dict) -> InputFingerprint:
        return cls(
            path=data["path"],
            size=data["size"],
            mtime_ns=data["mtime_ns"],
            sha256=data["sha256"],
        )


def resolve(path: str, workspace_root: pathlib.Path) -> pathlib.Path:
    """Where *path* lives on this machine.

    A declared input is relative to the workspace root unless it was outside it
    at capture time, in which case it was stored absolute -- and an absolute one
    is exactly the input that does not survive being moved to another machine.
    That asymmetry is deliberate: R11 wants portable fingerprints, and a path
    that cannot be portable says so by being absolute rather than by being
    silently reinterpreted against a different root.
    """
    candidate = pathlib.Path(path)
    return candidate if candidate.is_absolute() else workspace_root / candidate


def capture(path: str, workspace_root: pathlib.Path) -> InputFingerprint:
    """Fingerprint *path* as it is right now.

    Raises:
        SchemaError: the file is missing or unreadable. Loud rather than
            skipped: this runs at ingest, moments after the provider read the
            file, so a failure here is a declaration bug -- a unit naming an
            input it never read. Recording a bucket as tracked while quietly
            dropping one of its inputs is the untracked-but-not-declared state
            R9 exists to forbid.
    """
    resolved = resolve(path, workspace_root)
    try:
        stat = resolved.stat()
        digest = hashlib.sha256()
        with resolved.open("rb") as handle:
            while chunk := handle.read(_CHUNK):
                digest.update(chunk)
    except OSError as exc:
        raise SchemaError(
            f"Cannot fingerprint declared input {path!r} ({resolved}): {exc}. "
            "A unit may only declare inputs it actually read; anything else "
            "belongs in its `untracked` list (R9)."
        ) from exc

    return InputFingerprint(
        path=path,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        sha256=digest.hexdigest(),
    )
