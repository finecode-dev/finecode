"""The ownership unit: what one bucket of facts was derived from (R6, R11).

``goals.md`` §4.6 defines the unit as *the provider's captured footprint*,
defaulting to a single file. This module makes that definition a value the
provider hands to ``ingest``, so a bucket knows its own inputs and a later
cold start can re-check them (§4.8).

**A leaf module**: it imports only ``errors``, because both the store (which
holds units) and the providers (which build them) need to spell one.
"""

from __future__ import annotations

import dataclasses
import pathlib

from finecode_knowledge.model.errors import SchemaError
from finecode_knowledge.model.fingerprint import InputFingerprint, capture

__all__ = ["Unit", "relative_to_workspace"]

BucketKey = tuple[str, str]
"""``(provider_id, unit_id)`` -- how ``FactStore`` keys a bucket."""


@dataclasses.dataclass(frozen=True)
class Unit:
    """One ``(provider, unit)`` bucket's identity and its declared inputs."""

    provider_id: str
    unit_id: str
    """What the facts were derived from, rendered as a stable string -- usually
    a workspace-relative source path. Providers not broken down by source use
    one fused id for the whole provider (§4.6's multi-file unit)."""

    inputs: tuple[str, ...] = ()
    """Every file whose content changes these facts: the data the provider read,
    plus its code and schema modules (ADR-0023, ADR-0026).

    Workspace-relative POSIX where the file is under the root, absolute
    otherwise -- the frame ``SourceLoc`` already uses for a location outside
    every known project (ADR-0026 D3 via ADR-0023 D3). Absolute paths make the
    fact file non-portable, which is why the relative form is preferred and the
    absolute one is a fallback rather than a choice."""

    untracked: tuple[str, ...] = ()
    """Inputs that cannot be fingerprinted, named rather than pathed (R9).

    ``wm_registry`` reads the resolved config through an in-process API, so it
    declares ``"resolved config"`` here. A declared untracked input is
    permitted and penalized; an *undeclared* one is the silent defect R9
    exists to forbid, so this list is never a convenience."""

    depends_on: tuple[BucketKey, ...] = ()
    """Other buckets this unit's facts were derived from (ADR-0024 D2).

    One hop, checked for freshness rather than walked: if a depended-on bucket
    is unconfirmed, so is this one. ``ast_definitions``' bound-edge unit is the
    only user -- it binds ``calls`` edges against ``Handler`` facts
    ``wm_registry`` ingested, which no file fingerprint expresses. A transitive
    walk is the memo DAG's job."""

    fingerprints: tuple[InputFingerprint, ...] = ()
    """Each declared input as it stood when these facts were extracted (R11).

    Empty until ``captured()`` runs, and empty is not a neutral state: a bucket
    that declares inputs but carries no fingerprints has nothing to check
    against, so it can never be confirmed. That is the same verdict a fact file
    written before R11 produces, and deliberately so -- an uncaptured unit and a
    pre-R11 file are the same situation."""

    def __post_init__(self) -> None:
        if not self.unit_id:
            raise SchemaError(
                f"{self.provider_id}: a unit needs a non-empty unit_id. It is half the "
                "bucket key, so an empty one silently merges unrelated units."
            )

    @property
    def key(self) -> BucketKey:
        return (self.provider_id, self.unit_id)

    def captured(self, workspace_root: pathlib.Path) -> Unit:
        """This unit with every declared input fingerprinted as it is now.

        Called at ingest, when the provider has just read these files. The
        capture happens **here rather than inside the store** so that
        ``FactStore`` touches no filesystem: a store is constructed freely in
        tests and holds no notion of a workspace root, and giving it one to
        stat files would make every store construction location-dependent.
        """
        return dataclasses.replace(
            self,
            fingerprints=tuple(capture(path, workspace_root) for path in self.inputs),
        )

    def to_json(self) -> dict:
        return {
            "inputs": list(self.inputs),
            "untracked": list(self.untracked),
            "depends_on": [list(key) for key in self.depends_on],
            "fingerprints": [f.to_json() for f in self.fingerprints],
        }

    @classmethod
    def from_json(cls, provider_id: str, unit_id: str, data: dict) -> Unit:
        """Rebuild a unit from a bucket header.

        Every field defaults to empty, which is what makes a **pre-R11 fact
        file** load rather than fail: its headers carry only ``provider`` /
        ``unit`` / ``count``, so the unit comes back declaring nothing, and a
        unit declaring nothing cannot be confirmed. Refusing to load would turn
        a stale-but-usable store into no store at all, for a file the previous
        release wrote.
        """
        return cls(
            provider_id=provider_id,
            unit_id=unit_id,
            inputs=tuple(data.get("inputs", ())),
            untracked=tuple(data.get("untracked", ())),
            depends_on=tuple(tuple(key) for key in data.get("depends_on", ())),
            fingerprints=tuple(
                InputFingerprint.from_json(f) for f in data.get("fingerprints", ())
            ),
        )


def relative_to_workspace(path: pathlib.Path, workspace_root: pathlib.Path) -> str:
    """Spell *path* for ``Unit.inputs``: workspace-relative POSIX, else absolute.

    The fallback is not an edge case to design away -- a provider installed as
    a wheel outside the workspace has its code there, and ADR-0023 D2 keeps
    that on the same code path as an editable one rather than branching on
    install mode.
    """
    resolved = path.resolve()
    try:
        return resolved.relative_to(workspace_root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()
