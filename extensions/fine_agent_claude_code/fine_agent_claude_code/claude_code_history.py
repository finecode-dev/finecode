"""Locate Claude Code native session history for one captured run."""

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
    "ClaudeObservationState",
    "ClaudeSessionRecord",
    "locate_claude_history",
]


class ClaudeSessionRecord(typing.TypedDict, total=False):
    sessionId: str
    agentId: str


@dataclasses.dataclass
class ClaudeObservationState:
    session_id: str | None
    usage_provenance: UsageProvenance
    gaps: list[str]


def _unknown_provenance() -> UsageProvenance:
    return UsageProvenance(
        token_source=UsageSource.UNKNOWN,
        token_scope=UsageScope.UNKNOWN,
        cost_source=UsageSource.UNKNOWN,
        cost_scope=UsageScope.UNKNOWN,
    )


def _record_matches(record: dict[str, object], session_id: str) -> bool:
    value = record.get("sessionId")
    return isinstance(value, str) and value == session_id


def _file_has_matching_record(path: pathlib.Path, session_id: str) -> bool | None:
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
                    continue
                record: ClaudeSessionRecord = {}
                session_value = entry.get("sessionId")
                agent_value = entry.get("agentId")
                if isinstance(session_value, str):
                    record["sessionId"] = session_value
                if isinstance(agent_value, str):
                    record["agentId"] = agent_value
                if _record_matches(record, session_id):
                    return True
            return False
    except OSError:
        return None


def _is_supported_jsonl(path: pathlib.Path) -> bool | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except ValueError:
                    return False
                return isinstance(entry, dict)
            return False
    except OSError:
        return None


def _related_session_id(path: pathlib.Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                agent_value = entry.get("agentId")
                if isinstance(agent_value, str):
                    return agent_value
                session_value = entry.get("sessionId")
                if isinstance(session_value, str):
                    return session_value
            return None
    except OSError:
        return None


def locate_claude_history(
    state: ClaudeObservationState,
    native_root: pathlib.Path,
) -> AgentRunObservation:
    gaps = list(state.gaps)
    if state.session_id is None:
        gaps.append("claude reported no native session id")
        return AgentRunObservation(
            backend=AgentBackend.CLAUDE_CODE,
            backend_version=None,
            session_id=None,
            history_root=None,
            history_status=HistoryDiscoveryStatus.UNAVAILABLE,
            files=[],
            related_status=RelatedHistoryStatus.UNAVAILABLE,
            usage_provenance=state.usage_provenance,
            gaps=gaps,
        )
    candidates = [
        path
        for path in native_root.glob(f"projects/*/{state.session_id}.jsonl")
        if path.is_file() and not path.is_symlink()
    ]
    if not candidates:
        gaps.append(f"claude session file not found: {state.session_id}")
        return AgentRunObservation(
            backend=AgentBackend.CLAUDE_CODE,
            backend_version=None,
            session_id=state.session_id,
            history_root=None,
            history_status=HistoryDiscoveryStatus.UNAVAILABLE,
            files=[],
            related_status=RelatedHistoryStatus.UNAVAILABLE,
            usage_provenance=state.usage_provenance,
            gaps=gaps,
        )
    if len(candidates) > 1:
        gaps.append(
            f"multiple claude session files identify session {state.session_id}"
        )
        return AgentRunObservation(
            backend=AgentBackend.CLAUDE_CODE,
            backend_version=None,
            session_id=state.session_id,
            history_root=path_to_resource_uri(native_root),
            history_status=HistoryDiscoveryStatus.FAILED,
            files=[],
            related_status=RelatedHistoryStatus.UNAVAILABLE,
            usage_provenance=state.usage_provenance,
            gaps=gaps,
        )
    main_path = candidates[0]
    match = _file_has_matching_record(main_path, state.session_id)
    if match is None:
        gaps.append(f"claude session file is not supported jsonl: {main_path.name}")
        return AgentRunObservation(
            backend=AgentBackend.CLAUDE_CODE,
            backend_version=None,
            session_id=state.session_id,
            history_root=path_to_resource_uri(native_root),
            history_status=HistoryDiscoveryStatus.UNAVAILABLE,
            files=[],
            related_status=RelatedHistoryStatus.UNAVAILABLE,
            usage_provenance=state.usage_provenance,
            gaps=gaps,
        )
    if match is False:
        gaps.append(
            f"claude session records do not identify session {state.session_id}"
        )
        return AgentRunObservation(
            backend=AgentBackend.CLAUDE_CODE,
            backend_version=None,
            session_id=state.session_id,
            history_root=path_to_resource_uri(native_root),
            history_status=HistoryDiscoveryStatus.FAILED,
            files=[],
            related_status=RelatedHistoryStatus.UNAVAILABLE,
            usage_provenance=state.usage_provenance,
            gaps=gaps,
        )
    related_files: list[NativeHistoryFile] = []
    related_status = RelatedHistoryStatus.SCANNED
    sibling = main_path.parent / state.session_id
    if sibling.is_dir() and not sibling.is_symlink():
        for path in sorted(sibling.rglob("*.jsonl")):
            if not path.is_file() or path.is_symlink():
                continue
            supported = _is_supported_jsonl(path)
            if supported is None:
                gaps.append(f"claude related history unreadable: {path.name}")
                related_status = RelatedHistoryStatus.FAILED
                continue
            if supported is False:
                gaps.append(
                    f"claude related history is not supported jsonl: {path.name}"
                )
                related_status = RelatedHistoryStatus.FAILED
                continue
            try:
                relative = path.relative_to(native_root).as_posix()
            except ValueError:
                relative = path.name
            related_files.append(
                NativeHistoryFile(
                    role=NativeHistoryRole.RELATED,
                    source_uri=path_to_resource_uri(path),
                    native_relative_path=relative,
                    native_session_id=_related_session_id(path),
                    format=NativeHistoryFormat.CLAUDE_CODE,
                )
            )
    try:
        main_relative = main_path.relative_to(native_root).as_posix()
    except ValueError:
        main_relative = main_path.name
    history_status = (
        HistoryDiscoveryStatus.FAILED
        if related_status is RelatedHistoryStatus.FAILED
        else HistoryDiscoveryStatus.AVAILABLE
    )
    return AgentRunObservation(
        backend=AgentBackend.CLAUDE_CODE,
        backend_version=None,
        session_id=state.session_id,
        history_root=path_to_resource_uri(native_root),
        history_status=history_status,
        files=[
            NativeHistoryFile(
                role=NativeHistoryRole.MAIN,
                source_uri=path_to_resource_uri(main_path),
                native_relative_path=main_relative,
                native_session_id=state.session_id,
                format=NativeHistoryFormat.CLAUDE_CODE,
            ),
            *related_files,
        ],
        related_status=related_status,
        usage_provenance=state.usage_provenance,
        gaps=gaps,
    )
