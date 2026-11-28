"""Locate Codex native rollout history for one captured run."""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import typing

from fine_agent.history_search import files_containing
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
    "CodexMetaLine",
    "CodexObservationState",
    "CodexRolloutFacts",
    "CodexSessionMeta",
    "codex_home",
    "find_codex_thread_by_marker",
    "locate_codex_history",
    "read_rollout_facts",
]


class CodexSessionMeta(typing.TypedDict, total=False):
    id: str
    session_id: str
    parent_thread_id: str
    cli_version: str


class CodexMetaLine(typing.TypedDict, total=False):
    type: str
    payload: CodexSessionMeta


@dataclasses.dataclass
class CodexObservationState:
    thread_id: str | None
    usage_provenance: UsageProvenance
    gaps: list[str]


@dataclasses.dataclass(frozen=True)
class CodexRolloutFacts:
    """Run facts the rollout records but the `exec --json` stream omits.

    `None` means the rollout did not say. A model that changes between turns
    is `None` too, as `AgentRunUsage.combine` treats differing models.
    """

    provider: str | None
    model: str | None
    turns: int | None
    """Model responses, one `token_count` event each."""


def read_rollout_facts(path: pathlib.Path) -> CodexRolloutFacts:
    provider: str | None = None
    models: set[str] = set()
    turns = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                payload = entry.get("payload")
                if not isinstance(payload, dict):
                    continue
                entry_type = entry.get("type")
                if entry_type == "session_meta":
                    value = payload.get("model_provider")
                    if isinstance(value, str) and value:
                        provider = value
                elif entry_type == "turn_context":
                    value = payload.get("model")
                    if isinstance(value, str) and value:
                        models.add(value)
                elif (
                    entry_type == "event_msg"
                    and payload.get("type") == "token_count"
                    and payload.get("info") is not None
                ):
                    turns += 1
    except OSError:
        return CodexRolloutFacts(provider=None, model=None, turns=None)
    return CodexRolloutFacts(
        provider=provider,
        model=models.pop() if len(models) == 1 else None,
        turns=turns or None,
    )


def codex_home() -> pathlib.Path:
    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        return pathlib.Path(env_home)
    return pathlib.Path.home() / ".codex"


def _unknown_provenance() -> UsageProvenance:
    return UsageProvenance(
        token_source=UsageSource.UNKNOWN,
        token_scope=UsageScope.UNKNOWN,
        cost_source=UsageSource.UNKNOWN,
        cost_scope=UsageScope.UNKNOWN,
    )


def _read_meta(path: pathlib.Path) -> CodexSessionMeta | None:
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
                if entry.get("type") != "session_meta":
                    continue
                payload = entry.get("payload")
                if not isinstance(payload, dict):
                    return None
                meta: CodexSessionMeta = {}
                for key in ("id", "session_id", "parent_thread_id", "cli_version"):
                    value = payload.get(key)
                    if isinstance(value, str):
                        meta[key] = value
                return meta
            return None
    except OSError:
        return None


def _filename_thread_id(path: pathlib.Path) -> str | None:
    name = path.name
    if not name.startswith("rollout-") or not name.endswith(".jsonl"):
        return None
    stem = name[len("rollout-") : -len(".jsonl")]
    if len(stem) <= 20:
        return None
    return stem[20:]


def _iter_rollouts(root: pathlib.Path) -> list[pathlib.Path]:
    found: list[pathlib.Path] = []
    for sub in ("sessions", "archived_sessions"):
        base = root / sub
        if not base.is_dir():
            continue
        found.extend(
            path
            for path in sorted(base.rglob("rollout-*.jsonl"))
            if path.is_file() and not path.is_symlink()
        )
    return found


def locate_codex_history(
    state: CodexObservationState,
    native_root: pathlib.Path,
) -> AgentRunObservation:
    gaps = list(state.gaps)
    if state.thread_id is None:
        gaps.append("codex reported no thread id")
        return AgentRunObservation(
            backend=AgentBackend.CODEX,
            backend_version=None,
            session_id=None,
            history_root=None,
            history_status=HistoryDiscoveryStatus.UNAVAILABLE,
            files=[],
            related_status=RelatedHistoryStatus.UNAVAILABLE,
            usage_provenance=state.usage_provenance,
            gaps=gaps,
        )
    candidates: list[tuple[pathlib.Path, CodexSessionMeta]] = []
    for path in _iter_rollouts(native_root):
        filename_id = _filename_thread_id(path)
        if filename_id != state.thread_id:
            continue
        meta = _read_meta(path)
        if meta is None:
            continue
        if meta.get("id") != state.thread_id:
            continue
        candidates.append((path, meta))
    if not candidates:
        gaps.append(f"codex rollout not found for thread {state.thread_id}")
        return AgentRunObservation(
            backend=AgentBackend.CODEX,
            backend_version=None,
            session_id=state.thread_id,
            history_root=None,
            history_status=HistoryDiscoveryStatus.UNAVAILABLE,
            files=[],
            related_status=RelatedHistoryStatus.UNAVAILABLE,
            usage_provenance=state.usage_provenance,
            gaps=gaps,
        )
    if len(candidates) > 1:
        gaps.append(f"multiple codex rollouts identify thread {state.thread_id}")
        return AgentRunObservation(
            backend=AgentBackend.CODEX,
            backend_version=None,
            session_id=state.thread_id,
            history_root=path_to_resource_uri(native_root),
            history_status=HistoryDiscoveryStatus.FAILED,
            files=[],
            related_status=RelatedHistoryStatus.UNAVAILABLE,
            usage_provenance=state.usage_provenance,
            gaps=gaps,
        )
    main_path, main_meta = candidates[0]
    version = main_meta.get("cli_version")
    by_id: dict[str, tuple[pathlib.Path, CodexSessionMeta]] = {}
    for path in _iter_rollouts(native_root):
        meta = _read_meta(path)
        if meta is None:
            continue
        meta_id = meta.get("id")
        if isinstance(meta_id, str) and path not in by_id:
            by_id[meta_id] = (path, meta)
    related_files: list[NativeHistoryFile] = []
    related_status = RelatedHistoryStatus.SCANNED
    visited: set[str] = {state.thread_id}
    queue: list[str] = [state.thread_id]
    while queue:
        current = queue.pop(0)
        for meta_id, (path, meta) in sorted(by_id.items()):
            if meta_id in visited:
                continue
            if meta.get("parent_thread_id") != current:
                continue
            visited.add(meta_id)
            queue.append(meta_id)
            try:
                relative = path.relative_to(native_root).as_posix()
            except ValueError:
                relative = path.name
            related_files.append(
                NativeHistoryFile(
                    role=NativeHistoryRole.RELATED,
                    source_uri=path_to_resource_uri(path),
                    native_relative_path=relative,
                    native_session_id=meta_id,
                    format=NativeHistoryFormat.CODEX,
                )
            )
    try:
        main_relative = main_path.relative_to(native_root).as_posix()
    except ValueError:
        main_relative = main_path.name
    return AgentRunObservation(
        backend=AgentBackend.CODEX,
        backend_version=version,
        session_id=state.thread_id,
        history_root=path_to_resource_uri(native_root),
        history_status=HistoryDiscoveryStatus.AVAILABLE,
        files=[
            NativeHistoryFile(
                role=NativeHistoryRole.MAIN,
                source_uri=path_to_resource_uri(main_path),
                native_relative_path=main_relative,
                native_session_id=state.thread_id,
                format=NativeHistoryFormat.CODEX,
            ),
            *related_files,
        ],
        related_status=related_status,
        usage_provenance=state.usage_provenance,
        gaps=gaps,
    )


def find_codex_thread_by_marker(
    native_root: pathlib.Path, marker: str
) -> tuple[str | None, list[str]]:
    """The id of the one root thread whose rollout contains *marker*, and gaps.

    A spawned thread can carry its parent's history, so a marker found in a
    root thread and in threads below it identifies the root thread.
    """
    found, unreadable = files_containing(_iter_rollouts(native_root), marker)
    gaps = [f"codex rollout unreadable: {path.name}" for path in unreadable]
    matches: list[tuple[str, CodexSessionMeta]] = []
    for path in found:
        meta = _read_meta(path)
        thread_id = meta.get("id") if meta is not None else None
        if meta is not None and thread_id is not None:
            matches.append((thread_id, meta))
    roots = [match for match in matches if "parent_thread_id" not in match[1]]
    chosen = matches if len(matches) == 1 else roots
    if len(chosen) == 1:
        return chosen[0][0], gaps
    if not matches:
        gaps.append(f"no codex rollout contains marker {marker}")
    else:
        ids = ", ".join(sorted(thread_id for thread_id, _meta in matches))
        gaps.append(f"multiple codex threads contain marker {marker}: {ids}")
    return None, gaps
