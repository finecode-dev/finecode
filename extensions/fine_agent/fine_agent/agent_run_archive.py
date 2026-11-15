"""Copy one agent run's request, native history and accounting into a directory.

Everything here is knowable from a `run_agent_task` call alone, so any caller
that keeps a durable record of an agent run lays that part out the same way:
`request.json` and `history/` under the directory it owns. What else the
directory holds, and where it lives, stays the caller's decision.

Files are written create-only and copied files are checked for change during
the copy; a file that cannot be saved is reported in its entry and in
`errors`, never raised.
"""

from __future__ import annotations

import enum
import hashlib
import json
import pathlib
import typing

from finecode_extension_api.resource_uri import (
    path_to_resource_uri,
    resource_uri_to_path,
)

from fine_agent.history_types import (
    AgentBackend,
    AgentRunObservation,
    HistoryDiscoveryStatus,
    NativeHistoryFormat,
    NativeHistoryRole,
    RelatedHistoryStatus,
    UsageScope,
    UsageSource,
)
from fine_agent.run_agent_task_action import (
    AgentRunStatus,
    AgentRunUsage,
    RunAgentTaskRunPayload,
    RunAgentTaskRunResult,
)

__all__ = [
    "AgentRunArchive",
    "ArchivedAgent",
    "ArchivedFile",
    "ArchivedHistory",
    "ArchivedRequest",
    "ArchivedUsage",
    "ArchivedUsageProvenance",
    "FileSaveStatus",
    "archive_agent_run",
    "archive_observed_history",
    "write_archive_file",
]

_REQUEST_PATH = "request.json"
_MAIN_HISTORY_PATH = "history/main.jsonl"
_RELATED_HISTORY_DIR = "history/related"


class FileSaveStatus(enum.StrEnum):
    SAVED = "saved"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class ArchivedFile(typing.TypedDict):
    relative_path: str
    source_uri: str | None
    native_session_id: str | None
    format: NativeHistoryFormat | None
    status: FileSaveStatus
    byte_count: int | None
    sha256: str | None
    error: str | None


class ArchivedRequest(typing.TypedDict):
    schema_version: typing.Literal[1]
    prompt: str | None
    profile: str | None
    output_schema: dict[str, typing.Any] | None


class ArchivedAgent(typing.TypedDict):
    backend: AgentBackend | None
    backend_version: str | None
    profile: str | None
    provider: str | None
    model: str | None
    native_session_id: str | None
    turns: int | None


class ArchivedUsage(typing.TypedDict):
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    total_tokens: int | None
    approx_cost_usd: float | None
    provider: str | None
    model: str | None


class ArchivedUsageProvenance(typing.TypedDict):
    token_source: UsageSource
    token_scope: UsageScope
    cost_source: UsageSource
    cost_scope: UsageScope


class ArchivedHistory(typing.TypedDict):
    discovery_status: HistoryDiscoveryStatus | None
    related_status: RelatedHistoryStatus | None
    files: list[ArchivedFile]
    gaps: list[str]


class AgentRunArchive(typing.TypedDict):
    request: ArchivedFile
    agent: ArchivedAgent
    status: AgentRunStatus | None
    error: str | None
    duration_sec: float | None
    usage: ArchivedUsage | None
    usage_provenance: ArchivedUsageProvenance
    history: ArchivedHistory
    errors: list[str]
    """Every file that was not saved and every history gap.

    Empty exactly when the record is complete: the request and the main
    history were saved and nothing about the run went unobserved.
    """


def _file_entry(relative: str) -> ArchivedFile:
    return {
        "relative_path": relative,
        "source_uri": None,
        "native_session_id": None,
        "format": None,
        "status": FileSaveStatus.FAILED,
        "byte_count": None,
        "sha256": None,
        "error": None,
    }


def write_archive_file(
    run_dir: pathlib.Path, relative: str, data: bytes
) -> ArchivedFile:
    """Write *data* create-only at *relative* under *run_dir* and describe it.

    An existing destination is a failure, not an overwrite: two writers of one
    archive path mean the archive is no longer one run's record.
    """
    entry = _file_entry(relative)
    try:
        with open(run_dir / relative, "xb") as handle:
            _ = handle.write(data)
    except FileExistsError as error:
        entry["error"] = f"destination already exists: {error}"
        return entry
    except OSError as error:
        entry["error"] = str(error)
        return entry
    entry["status"] = FileSaveStatus.SAVED
    entry["byte_count"] = len(data)
    entry["sha256"] = hashlib.sha256(data).hexdigest()
    return entry


def _check_destination(
    run_dir: pathlib.Path, relative: str, seen: set[str]
) -> str | None:
    if not relative or pathlib.PurePosixPath(relative).is_absolute():
        return f"absolute destination-relative path: {relative!r}"
    pure = pathlib.PurePosixPath(relative)
    if ".." in pure.parts:
        return f"traversing destination-relative path: {relative!r}"
    if relative in seen:
        return f"duplicate destination-relative path: {relative!r}"
    seen.add(relative)
    current = run_dir
    for part in pathlib.Path(relative).parts[:-1]:
        current = current / part
        try:
            if current.is_symlink():
                return f"symlinked archive parent: {current}"
        except OSError:
            continue
    return None


def _copy_history_file(
    run_dir: pathlib.Path,
    relative: str,
    source: pathlib.Path,
    history_root: pathlib.Path | None,
    seen: set[str],
) -> ArchivedFile:
    entry = _file_entry(relative)
    problem = _check_destination(run_dir, relative, seen)
    if problem is not None:
        entry["error"] = problem
        return entry
    try:
        entry["source_uri"] = str(path_to_resource_uri(source))
    except (OSError, ValueError):
        entry["source_uri"] = None
    if history_root is not None:
        try:
            _ = source.resolve().relative_to(history_root.resolve())
        except (OSError, ValueError):
            entry["error"] = f"source is outside the native history root: {source}"
            return entry
    try:
        if source.is_symlink() or not source.is_file():
            entry["error"] = f"source is not a regular file: {source}"
            return entry
        before = source.stat()
        data = source.read_bytes()
        after = source.stat()
    except OSError as error:
        entry["error"] = str(error)
        return entry
    # The backend may still be appending to its session file; a copy taken
    # mid-write is a truncated record that would read as a complete one.
    if before.st_size != after.st_size or before.st_mtime != after.st_mtime:
        entry["error"] = f"source changed during copy: {source}"
        return entry
    destination = run_dir / pathlib.Path(relative)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        entry["error"] = str(error)
        return entry
    written = write_archive_file(run_dir, relative, data)
    written["source_uri"] = entry["source_uri"]
    return written


def _archive_history(
    run_dir: pathlib.Path,
    request: RunAgentTaskRunPayload | None,
    result: RunAgentTaskRunResult | None,
) -> ArchivedHistory:
    history: ArchivedHistory = {
        "discovery_status": None,
        "related_status": None,
        "files": [],
        "gaps": [],
    }
    if request is None:
        history["gaps"].append("agent not dispatched")
        return history
    if result is None:
        history["gaps"].append("agent dispatched but returned no result")
        return history
    observation = result.observation
    if observation is None:
        history["gaps"].append("observation not collected")
        return history
    return archive_observed_history(run_dir, observation)


def archive_observed_history(
    run_dir: pathlib.Path, observation: AgentRunObservation
) -> ArchivedHistory:
    """Copy the files *observation* names into `history/` under *run_dir*.

    The main file lands at `history/main.jsonl` and related files under
    `history/related/` at their native relative path, create-only. This is the
    part of `archive_agent_run` that needs no request, so a session nobody
    dispatched is archived in the same layout. *run_dir* must already exist.
    """
    history: ArchivedHistory = {
        "discovery_status": None,
        "related_status": None,
        "files": [],
        "gaps": [],
    }
    history["discovery_status"] = observation.history_status
    history["related_status"] = observation.related_status
    history["gaps"].extend(observation.gaps)
    history_root: pathlib.Path | None = None
    if observation.history_root is not None:
        try:
            history_root = resource_uri_to_path(observation.history_root)
        except ValueError:
            history_root = None
    seen: set[str] = set()
    main_seen = False
    for native in observation.files:
        if native.role is NativeHistoryRole.MAIN:
            main_seen = True
            relative = _MAIN_HISTORY_PATH
        else:
            relative = f"{_RELATED_HISTORY_DIR}/{native.native_relative_path}"
        try:
            source_path = resource_uri_to_path(native.source_uri)
        except ValueError as error:
            entry = _file_entry(relative)
            entry["error"] = str(error)
        else:
            entry = _copy_history_file(
                run_dir, relative, source_path, history_root, seen
            )
        entry["native_session_id"] = native.native_session_id
        entry["format"] = native.format
        if entry["source_uri"] is None:
            entry["source_uri"] = str(native.source_uri)
        history["files"].append(entry)
    if not main_seen:
        entry = _file_entry(_MAIN_HISTORY_PATH)
        entry["status"] = FileSaveStatus.UNAVAILABLE
        entry["error"] = "main history was not collected"
        history["files"].append(entry)
    return history


def _usage_record(usage: AgentRunUsage | None) -> ArchivedUsage | None:
    if usage is None:
        return None
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "total_tokens": usage.total_tokens,
        "approx_cost_usd": usage.approx_cost_usd,
        "provider": usage.provider,
        "model": usage.model,
    }


def archive_agent_run(
    run_dir: pathlib.Path,
    request: RunAgentTaskRunPayload | None,
    result: RunAgentTaskRunResult | None,
) -> AgentRunArchive:
    """Write `request.json` and the observed native history into *run_dir*.

    *request* is `None` when no agent was dispatched, *result* is `None` when
    one was dispatched but returned nothing (it was interrupted); both are
    recorded as history gaps rather than refused, so an incomplete run still
    leaves a record of how far it got. *run_dir* must already exist.
    """
    request_record: ArchivedRequest = {
        "schema_version": 1,
        "prompt": request.prompt if request is not None else None,
        "profile": request.profile if request is not None else None,
        "output_schema": request.output_schema if request is not None else None,
    }
    request_entry = write_archive_file(
        run_dir,
        _REQUEST_PATH,
        json.dumps(request_record, sort_keys=True, indent=2).encode("utf-8"),
    )
    history = _archive_history(run_dir, request, result)

    agent: ArchivedAgent = {
        "backend": None,
        "backend_version": None,
        "profile": request_record["profile"],
        "provider": None,
        "model": None,
        "native_session_id": None,
        "turns": None,
    }
    provenance: ArchivedUsageProvenance = {
        "token_source": UsageSource.UNKNOWN,
        "token_scope": UsageScope.UNKNOWN,
        "cost_source": UsageSource.UNKNOWN,
        "cost_scope": UsageScope.UNKNOWN,
    }
    usage = _usage_record(result.usage) if result is not None else None
    if usage is not None:
        agent["provider"] = usage["provider"]
        agent["model"] = usage["model"]
    if result is not None:
        agent["turns"] = result.turns
        if result.observation is not None:
            observation = result.observation
            agent["backend"] = observation.backend
            agent["backend_version"] = observation.backend_version
            agent["native_session_id"] = observation.session_id
            provenance = {
                "token_source": observation.usage_provenance.token_source,
                "token_scope": observation.usage_provenance.token_scope,
                "cost_source": observation.usage_provenance.cost_source,
                "cost_scope": observation.usage_provenance.cost_scope,
            }

    errors: list[str] = []
    if request_entry["error"] is not None:
        errors.append(f"request write failed: {request_entry['error']}")
    for entry in history["files"]:
        if entry["status"] is not FileSaveStatus.SAVED:
            errors.append(f"{entry['relative_path']}: {entry['error'] or 'not saved'}")
    errors.extend(history["gaps"])

    return {
        "request": request_entry,
        "agent": agent,
        "status": result.status if result is not None else None,
        "error": result.error if result is not None else None,
        "duration_sec": result.duration_sec if result is not None else None,
        "usage": usage,
        "usage_provenance": provenance,
        "history": history,
        "errors": errors,
    }
