"""Locate Pi native session history for one captured run."""

from __future__ import annotations

import dataclasses
import json
import pathlib
import typing

from fine_agent.history_types import (
    AgentBackend,
    AgentRunObservation,
    HistoryDiscoveryStatus,
    NativeHistoryFile,
    NativeHistoryFormat,
    NativeHistoryRole,
    RelatedHistoryStatus,
    UsageProvenance,
    UsageScope,
    UsageSource,
)
from finecode_extension_api.resource_uri import path_to_resource_uri

__all__ = [
    "PiObservationState",
    "PiSessionHeader",
    "locate_pi_history",
]


class PiSessionHeader(typing.TypedDict, total=False):
    type: str
    id: str
    version: int


@dataclasses.dataclass
class PiObservationState:
    session_id: str | None
    session_file: str | None
    usage_provenance: UsageProvenance
    gaps: list[str]


def _unknown_provenance() -> UsageProvenance:
    return UsageProvenance(
        token_source=UsageSource.UNKNOWN,
        token_scope=UsageScope.UNKNOWN,
        cost_source=UsageSource.UNKNOWN,
        cost_scope=UsageScope.UNKNOWN,
    )


def _read_header(path: pathlib.Path) -> PiSessionHeader | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except ValueError:
                    return None
                if not isinstance(entry, dict):
                    return None
                header: PiSessionHeader = {}
                entry_type = entry.get("type")
                entry_id = entry.get("id")
                entry_version = entry.get("version")
                if isinstance(entry_type, str):
                    header["type"] = entry_type
                if isinstance(entry_id, str):
                    header["id"] = entry_id
                if isinstance(entry_version, int) and not isinstance(
                    entry_version, bool
                ):
                    header["version"] = entry_version
                return header
    except OSError:
        return None
    return None


def locate_pi_history(
    state: PiObservationState,
    native_root: pathlib.Path,
) -> AgentRunObservation:
    gaps = list(state.gaps)
    if state.session_id is None or state.session_file is None:
        gaps.append("pi reported no native session file")
        return AgentRunObservation(
            backend=AgentBackend.PI,
            backend_version=None,
            session_id=state.session_id,
            history_root=None,
            history_status=HistoryDiscoveryStatus.UNAVAILABLE,
            files=[],
            related_status=RelatedHistoryStatus.UNAVAILABLE,
            usage_provenance=state.usage_provenance,
            gaps=gaps,
        )
    candidate = pathlib.Path(state.session_file)
    if not candidate.is_absolute():
        candidate = native_root / candidate
    try:
        within_root = candidate.is_relative_to(native_root)
    except (OSError, ValueError):
        within_root = False
    history_root = native_root if within_root else candidate.parent
    if not candidate.is_file():
        gaps.append(f"pi session file not found: {candidate}")
        return AgentRunObservation(
            backend=AgentBackend.PI,
            backend_version=None,
            session_id=state.session_id,
            history_root=None,
            history_status=HistoryDiscoveryStatus.UNAVAILABLE,
            files=[],
            related_status=RelatedHistoryStatus.UNAVAILABLE,
            usage_provenance=state.usage_provenance,
            gaps=gaps,
        )
    header = _read_header(candidate)
    if (
        header is None
        or header.get("type") != "session"
        or header.get("id") != state.session_id
    ):
        gaps.append(f"pi session header does not identify session {state.session_id}")
        return AgentRunObservation(
            backend=AgentBackend.PI,
            backend_version=None,
            session_id=state.session_id,
            history_root=path_to_resource_uri(history_root),
            history_status=HistoryDiscoveryStatus.FAILED,
            files=[],
            related_status=RelatedHistoryStatus.UNAVAILABLE,
            usage_provenance=state.usage_provenance,
            gaps=gaps,
        )
    try:
        duplicates = _count_matching_sessions(
            native_root, state.session_id, exclude=candidate
        )
    except OSError as error:
        gaps.append(f"pi session discovery failed: {error}")
        duplicates = 0
    if duplicates > 0:
        gaps.append(f"multiple pi session files identify session {state.session_id}")
        return AgentRunObservation(
            backend=AgentBackend.PI,
            backend_version=None,
            session_id=state.session_id,
            history_root=path_to_resource_uri(history_root),
            history_status=HistoryDiscoveryStatus.FAILED,
            files=[],
            related_status=RelatedHistoryStatus.UNAVAILABLE,
            usage_provenance=state.usage_provenance,
            gaps=gaps,
        )
    try:
        relative = candidate.relative_to(history_root).as_posix()
    except ValueError:
        relative = candidate.name
    gaps.append("pi child session discovery is not available from session stats")
    return AgentRunObservation(
        backend=AgentBackend.PI,
        backend_version=None,
        session_id=state.session_id,
        history_root=path_to_resource_uri(history_root),
        history_status=HistoryDiscoveryStatus.AVAILABLE,
        files=[
            NativeHistoryFile(
                role=NativeHistoryRole.MAIN,
                source_uri=path_to_resource_uri(candidate),
                native_relative_path=relative,
                native_session_id=state.session_id,
                format=NativeHistoryFormat.PI,
            )
        ],
        related_status=RelatedHistoryStatus.UNAVAILABLE,
        usage_provenance=state.usage_provenance,
        gaps=gaps,
    )


def _count_matching_sessions(
    root: pathlib.Path, session_id: str, *, exclude: pathlib.Path
) -> int:
    count = 0
    try:
        exclude_resolved = exclude.resolve()
    except OSError:
        exclude_resolved = exclude
    for path in root.rglob("*.jsonl"):
        try:
            if path.resolve() == exclude_resolved:
                continue
        except OSError:
            continue
        if not path.is_file():
            continue
        header = _read_header(path)
        if (
            header is not None
            and header.get("type") == "session"
            and header.get("id") == session_id
        ):
            count += 1
            break
    return count
