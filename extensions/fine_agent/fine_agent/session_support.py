"""Shared pure helpers for native session references.

The helpers own the identity/backend/project checks and the settled-result
finalization shared by every backend handler and the router. They perform no
IO and start no processes; filesystem and subprocess lookups stay in the
backend handlers.
"""

from __future__ import annotations

import os.path
import pathlib
import re
import uuid

from finecode_extension_api.resource_uri import (
    ResourceUri,
    path_to_resource_uri,
    resource_uri_to_path,
)

from fine_agent.history_types import AgentBackend
from fine_agent.run_agent_task_action import (
    AgentRunStatus,
    AgentSessionRef,
    RunAgentTaskRunResult,
)

__all__ = [
    "session_backend_error",
    "session_id_error",
    "session_project_error",
    "with_session",
]

_FULL_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def session_id_error(session_id: str) -> str | None:
    """Reason the session ID is unusable, or `None` when it is a full UUID.

    Only the complete hyphenated spelling is accepted: empty, partial,
    brace/URN/compact spellings and opaque names are rejected. History
    locators and observation parsing keep accepting their own opaque IDs;
    this validates continuation references only.
    """
    if not _FULL_UUID_RE.match(session_id):
        return f"invalid session ID {session_id!r}: expected a full hyphenated UUID"
    return None


def session_backend_error(ref: AgentSessionRef, backend: AgentBackend) -> str | None:
    """Reason the reference targets another backend, or `None` on agreement."""
    if ref.backend is not backend:
        return (
            f"session {ref.session_id!r} belongs to backend {ref.backend.value}, "
            f"not {backend.value}"
        )
    return None


def _normalize_local_dir(path: pathlib.Path) -> str:
    """Lexically normalized directory spelling for comparison.

    Collapses `.`/`..` and OS path case without touching the filesystem, so
    symlinks keep the project identity the caller spelled.
    """
    return os.path.normcase(os.path.normpath(str(path)))


def session_project_error(
    ref: AgentSessionRef, project_dir: pathlib.Path
) -> str | None:
    """Reason the reference belongs to another project, or `None` on agreement."""
    try:
        ref_path = resource_uri_to_path(ref.project_dir)
    except ValueError:
        return (
            f"session {ref.session_id!r} belongs to non-local project "
            f"{ref.project_dir!r}"
        )
    if _normalize_local_dir(ref_path) != _normalize_local_dir(project_dir):
        return (
            f"session {ref.session_id!r} belongs to project "
            f"{ref.project_dir!r}, not {path_to_resource_uri(project_dir)!r}"
        )
    return None


def with_session(
    result: RunAgentTaskRunResult,
    *,
    backend: AgentBackend,
    project_dir: pathlib.Path,
    reported_id: str | None,
    expected: AgentSessionRef | None = None,
    report_unsettled_session: bool = False,
) -> RunAgentTaskRunResult:
    """Attach the reported native session, enforcing the settled-ID rule.

    `reported_id` is backend evidence from this run, never the requested or
    generated ID. On an otherwise-settled result a missing or unusable
    identity becomes `FAILED` with every other field preserved; on
    failed/refused/aborted results absence never replaces the existing
    outcome. A usable identity that disagrees with `expected` is never
    handed out as the requested session: a settled mismatch fails.
    """
    if result.status is not AgentRunStatus.SETTLED:
        if not report_unsettled_session:
            return result
        if reported_id is None or session_id_error(reported_id) is not None:
            return result
        if expected is not None and not _uuid_equals(
            reported_id, expected.session_id
        ):
            return result
        return _attach_session(result, backend, project_dir, reported_id)
    if reported_id is None or session_id_error(reported_id) is not None:
        return _replace(
            result, AgentRunStatus.FAILED, _missing_id_reason(backend, reported_id)
        )
    if expected is not None and not _uuid_equals(reported_id, expected.session_id):
        return _replace(
            result,
            AgentRunStatus.FAILED,
            f"backend {backend.value} resumed unexpected session "
            f"{reported_id!r}, expected {expected.session_id!r}",
        )
    return _attach_session(result, backend, project_dir, reported_id)


def _uuid_equals(left: str, right: str) -> bool:
    return uuid.UUID(left) == uuid.UUID(right)


def _missing_id_reason(backend: AgentBackend, reported_id: str | None) -> str:
    if reported_id is None:
        return (
            f"backend {backend.value} settled without reporting a session ID; "
            "no session reference is available"
        )
    return (
        f"backend {backend.value} reported unusable session ID "
        f"{reported_id!r}; no session reference is available"
    )


def _replace(
    result: RunAgentTaskRunResult, status: AgentRunStatus, error: str
) -> RunAgentTaskRunResult:
    result.status = status
    result.error = error
    result.session = None
    return result


def _attach_session(
    result: RunAgentTaskRunResult,
    backend: AgentBackend,
    project_dir: pathlib.Path,
    reported_id: str,
) -> RunAgentTaskRunResult:
    project_uri: ResourceUri = path_to_resource_uri(project_dir)
    result.session = AgentSessionRef(
        backend=backend, session_id=reported_id, project_dir=project_uri
    )
    return result
