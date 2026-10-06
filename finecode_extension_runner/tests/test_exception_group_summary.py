from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from finecode_extension_api import code_action

from finecode_extension_runner._services import run_action as run_action_service
from finecode_extension_runner.testing import handler_test_session


def _decode_error() -> UnicodeDecodeError:
    """A real cp1252 decode of UTF-8 content containing a curly quote — the
    exact shape a Windows box produces (issues #46/#47). The rendered text of
    ``UnicodeDecodeError`` varies across CPython versions (3.14 formats byte
    ranges differently), so tests assert against the exception's own ``str()``
    rather than hardcoding the spelling.
    """
    with pytest.raises(UnicodeDecodeError) as exc_info:
        "\u201d".encode("utf-8").decode("cp1252")
    return exc_info.value


def _expected_summary(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _classify(eg: BaseExceptionGroup) -> tuple[bool, str]:
    return run_action_service._classify_exception_group(eg)


def test_unknown_exception_is_named_in_the_summary() -> None:
    """An unexpected leaf exception must be surfaced as ``Type: message`` —
    not collapsed into the group's own opaque ``unhandled errors in a
    TaskGroup (N sub-exception)`` summary, which is what every wrapper above
    ``_classify_exception_group`` embeds into its error string.
    """
    error = _decode_error()
    eg = BaseExceptionGroup("unhandled errors in a TaskGroup", [error])

    is_cancelled, message = _classify(eg)

    assert is_cancelled is False
    assert message == _expected_summary(error)
    assert "unhandled errors in a TaskGroup" not in message


def test_nested_groups_are_flattened_to_their_leaf_exceptions() -> None:
    """A TaskGroup raises a group whose members can themselves be groups (a
    task that ran its own TaskGroup). The summary must name the deepest leaf,
    not the intermediate group wrappers.
    """
    eg = BaseExceptionGroup(
        "outer",
        [BaseExceptionGroup("inner", [_decode_error()])],
    )

    is_cancelled, message = _classify(eg)

    assert is_cancelled is False
    assert message.startswith("UnicodeDecodeError: ")
    assert "inner" not in message


def test_repeated_leaf_from_nested_wrapping_is_deduped() -> None:
    """The same exception can surface more than once when nested groups wrap
    it at each level; the summary lists it once.
    """
    error = _decode_error()
    eg = BaseExceptionGroup(
        "unhandled errors in a TaskGroup",
        [
            BaseExceptionGroup("branch-1", [error]),
            BaseExceptionGroup("branch-2", [error]),
        ],
    )

    is_cancelled, message = _classify(eg)

    assert is_cancelled is False
    assert message == _expected_summary(error)
    assert message.count("UnicodeDecodeError") == 1


def test_distinct_failures_with_equal_text_are_all_listed() -> None:
    """Deduplication is by identity, not text: two tasks failing with the same
    message are two failures, and the summary shows both.
    """
    first, second = _decode_error(), _decode_error()
    eg = BaseExceptionGroup("unhandled errors in a TaskGroup", [first, second])

    is_cancelled, message = _classify(eg)

    assert is_cancelled is False
    assert message == f"{_expected_summary(first)}; {_expected_summary(second)}"


def test_real_task_group_omits_cancelled_siblings() -> None:
    """CPython's TaskGroup leaves the siblings it cancelled out of the group it
    raises, so a real crash summarizes to just the failing leaf.
    """

    async def _boom() -> None:
        raise _decode_error()

    async def _slow() -> None:
        await asyncio.sleep(10)

    async def _run() -> None:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_boom())
            tg.create_task(_slow())

    with pytest.raises(BaseExceptionGroup) as exc_info:
        asyncio.run(_run())

    is_cancelled, message = _classify(exc_info.value)

    assert is_cancelled is False
    assert message == _expected_summary(_decode_error())


def test_unknown_leaf_with_cancelled_sibling_has_no_trailing_separator() -> None:
    """A group built outside asyncio.TaskGroup may carry a message-less
    CancelledError (``str()`` is ""); it must neither leak ``; `` into the
    summary nor be reported as a second crash.
    """
    error = _decode_error()
    eg = BaseExceptionGroup("g", [error, asyncio.CancelledError()])

    is_cancelled, message = _classify(eg)

    assert is_cancelled is False
    assert message == _expected_summary(error)


def test_message_less_cancelled_error_group_still_counts_as_cancellation() -> None:
    """A TaskGroup where only cancellations remain — e.g. every child was
    cancelled — is a benign cancellation (``all_cancelled``), not a crash.
    """
    eg = BaseExceptionGroup("g", [asyncio.CancelledError()])

    is_cancelled, _message = _classify(eg)

    assert is_cancelled is True


def test_known_failure_keeps_its_message_without_a_type_prefix() -> None:
    """An already-classified failure (``_is_known_failure``) propagates its
    own message verbatim — it was already summarized by a deeper layer.
    """
    known = run_action_service.ActionFailedException(
        "Running action handler 'pyrefly' failed(Run 0): boom"
    )
    eg = BaseExceptionGroup("g", [known])

    is_cancelled, message = _classify(eg)

    assert is_cancelled is False
    assert message == "Running action handler 'pyrefly' failed(Run 0): boom"
    assert "ActionFailedException" not in message


def test_known_failure_and_unknown_leaf_are_both_listed() -> None:
    known = run_action_service.ActionFailedException("wrapped failure")
    eg = BaseExceptionGroup("g", [known, _decode_error()])

    is_cancelled, message = _classify(eg)

    assert is_cancelled is False
    assert message == f"wrapped failure; {_expected_summary(_decode_error())}"


def test_known_failure_with_cancelled_sibling_has_no_trailing_separator() -> None:
    """A recognized failure whose siblings the TaskGroup cancelled must render
    without trailing ``; `` noise from the message-less CancelledError.
    """
    known = run_action_service.ActionFailedException("wrapped failure")
    eg = BaseExceptionGroup("g", [known, asyncio.CancelledError()])

    is_cancelled, message = _classify(eg)

    assert is_cancelled is False
    assert message == "wrapped failure"


def test_app_level_cancellation_is_still_all_cancelled() -> None:
    eg = BaseExceptionGroup(
        "g",
        [run_action_service.ActionCancelledException("cancelled by pyrefly")],
    )

    is_cancelled, message = _classify(eg)

    assert is_cancelled is True
    assert message == "cancelled by pyrefly"


# ---------------------------------------------------------------------------
# End-to-end: the CI shape. A handler whose own TaskGroup crashes with an
# unexpected exception used to surface as "Running action handler ... failed(
# Run N): unhandled errors in a TaskGroup (1 sub-exception)" — the real
# exception was buried in the ER log only.
# ---------------------------------------------------------------------------


class _ExceptionSummaryTestAction(code_action.Action):
    """Uses the base Action's default payload/run-context/result types — the
    tests only care about how the raised exception is summarized."""


class _TaskGroupUnicodeErrorHandler(
    code_action.ActionHandler[
        _ExceptionSummaryTestAction, code_action.ActionHandlerConfig
    ]
):
    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: code_action.RunActionContext,
    ) -> code_action.RunActionResult:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_raise_decode_error())
        return None  # pragma: no cover - unreachable, task group always raises


async def _raise_decode_error() -> None:
    raise _decode_error()


def _single_handler_action(action_name: str, handler_cls: type) -> dict[str, dict]:
    handler_source = f"{handler_cls.__module__}.{handler_cls.__qualname__}"
    action_source = (
        f"{_ExceptionSummaryTestAction.__module__}."
        f"{_ExceptionSummaryTestAction.__qualname__}"
    )
    return {
        action_name: {
            "source": action_source,
            "handlers": [{"name": handler_cls.__name__, "source": handler_source}],
        }
    }


@pytest.mark.asyncio
async def test_task_group_crash_in_handler_surfaces_the_real_exception(
    tmp_path: Path,
) -> None:
    """Regression test for the Windows CI `inspect_code` failure (issue #47):
    when an unexpected exception escapes a handler through a TaskGroup, the
    propagated ActionFailedException message must name the exception instead
    of the opaque group summary.
    """
    actions = _single_handler_action("boom_action", _TaskGroupUnicodeErrorHandler)
    async with handler_test_session(project_dir=tmp_path, actions=actions) as session:
        with pytest.raises(run_action_service.ActionFailedException) as exc_info:
            await session.run_action("boom_action")

    error = _decode_error()
    assert _expected_summary(error) in exc_info.value.message
    assert "unhandled errors in a TaskGroup" not in exc_info.value.message
