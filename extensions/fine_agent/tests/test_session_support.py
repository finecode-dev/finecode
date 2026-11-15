"""Pure session-reference rules, without any backend or filesystem."""

from __future__ import annotations

import pathlib

from finecode_extension_api.resource_uri import ResourceUri

from fine_agent.history_types import AgentBackend
from fine_agent.run_agent_task_action import (
    AgentRunStatus,
    AgentRunUsage,
    AgentSessionRef,
    RunAgentTaskRunResult,
)
from fine_agent.session_support import (
    session_backend_error,
    session_id_error,
    session_project_error,
    with_session,
)

_SESSION_ID = "11111111-2222-3333-8444-555555555555"
_PROJECT_DIR = pathlib.Path("/ws/proj")
_PROJECT_URI = ResourceUri("file:///ws/proj")


def _settled() -> RunAgentTaskRunResult:
    return RunAgentTaskRunResult(
        status=AgentRunStatus.SETTLED,
        output="the answer",
        structured_output={"answer": 42},
        turns=3,
        usage=AgentRunUsage(total_tokens=9000),
        duration_sec=42.0,
        observation=None,
    )


def _ref(
    session_id: str = _SESSION_ID,
    backend: AgentBackend = AgentBackend.PI,
    project_dir: ResourceUri = _PROJECT_URI,
) -> AgentSessionRef:
    return AgentSessionRef(
        backend=backend, session_id=session_id, project_dir=project_dir
    )


def test_full_uuid_spellings_are_accepted() -> None:
    """Identity compares by UUID value, not by letter case."""
    assert session_id_error(_SESSION_ID) is None
    assert session_id_error(_SESSION_ID.upper()) is None


def test_partial_and_non_uuid_spellings_are_rejected() -> None:
    """Names, fragments and packed forms are not session identities."""
    assert session_id_error("") is not None
    assert session_id_error("thread-1") is not None
    assert session_id_error(_SESSION_ID[:8]) is not None
    assert session_id_error("{" + _SESSION_ID + "}") is not None
    assert session_id_error("urn:uuid:" + _SESSION_ID) is not None
    assert session_id_error(_SESSION_ID.replace("-", "")) is not None


def test_backend_mismatch_names_both_sides() -> None:
    """A reference for another backend is never mistaken for this one."""
    error = session_backend_error(_ref(backend=AgentBackend.CODEX), AgentBackend.PI)

    assert error is not None
    assert "codex" in error
    assert "pi" in error
    assert session_backend_error(_ref(), AgentBackend.PI) is None


def test_project_comparison_ignores_lexical_segments() -> None:
    """Dot-dot spellings of the same directory still agree."""
    ref = _ref(project_dir=ResourceUri("file:///ws/proj"))

    assert session_project_error(ref, pathlib.Path("/ws/sub/../proj")) is None


def test_project_mismatch_names_the_expected_project() -> None:
    error = session_project_error(
        _ref(project_dir=ResourceUri("file:///elsewhere")), _PROJECT_DIR
    )

    assert error is not None
    assert "elsewhere" in error


def test_non_local_project_is_a_reasoned_failure() -> None:
    error = session_project_error(
        _ref(project_dir=ResourceUri("https://example.com/proj")), _PROJECT_DIR
    )

    assert error is not None


def test_settled_run_reports_the_backend_spelling() -> None:
    """The emitted reference keeps the backend-reported ID spelling."""
    upper = _SESSION_ID.upper()
    result = with_session(
        _settled(),
        backend=AgentBackend.PI,
        project_dir=_PROJECT_DIR,
        reported_id=upper,
    )

    assert result.status is AgentRunStatus.SETTLED
    assert result.session == AgentSessionRef(
        backend=AgentBackend.PI,
        session_id=upper,
        project_dir=ResourceUri("file:///ws/proj"),
    )
    assert result.output == "the answer"
    assert result.structured_output == {"answer": 42}
    assert result.turns == 3
    assert result.usage == AgentRunUsage(total_tokens=9000)
    assert result.duration_sec == 42.0


def test_settled_run_without_identity_fails_and_keeps_output() -> None:
    """A settled run with no reported ID fails without losing its text."""
    for reported in (None, "thread-1"):
        result = with_session(
            _settled(),
            backend=AgentBackend.PI,
            project_dir=_PROJECT_DIR,
            reported_id=reported,
        )

        assert result.status is AgentRunStatus.FAILED
        assert result.error is not None
        assert result.output == "the answer"
        assert result.session is None


def test_missing_identity_never_replaces_failure_or_refusal() -> None:
    """Absence explains a settled run only; other outcomes stand as-is."""
    for status in (
        AgentRunStatus.FAILED,
        AgentRunStatus.ABORTED,
        AgentRunStatus.REFUSED_INTERACTION,
    ):
        original = RunAgentTaskRunResult(status=status, error="kept", output="out")
        result = with_session(
            original,
            backend=AgentBackend.PI,
            project_dir=_PROJECT_DIR,
            reported_id=None,
        )

        assert result.status is status
        assert result.error == "kept"
        assert result.session is None


def test_unexpected_resumed_identity_fails_a_settled_run() -> None:
    """A different session is never handed out as the requested one."""
    expected = _ref()
    result = with_session(
        _settled(),
        backend=AgentBackend.PI,
        project_dir=_PROJECT_DIR,
        reported_id="22222222-2222-4222-8222-222222222222",
        expected=expected,
    )

    assert result.status is AgentRunStatus.FAILED
    assert "22222222" in (result.error or "")
    assert result.session is None


def test_matching_expected_identity_is_attached() -> None:
    result = with_session(
        _settled(),
        backend=AgentBackend.PI,
        project_dir=_PROJECT_DIR,
        reported_id=_SESSION_ID,
        expected=_ref(),
    )

    assert result.status is AgentRunStatus.SETTLED
    assert result.session == _ref()
