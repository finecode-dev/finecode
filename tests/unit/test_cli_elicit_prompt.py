"""The CLI's side of ``client/elicit``: one terminal, one question at a time.

The prompt shares a process with a run that is streaming its results, and it
shares a thread pool with nothing at all. Three properties follow, and none of
them shows up on the happy path:

* the question is written to stderr like everything else about the interaction,
  so ``finecode run ... > report.txt`` from a terminal produces the same bytes
  as one from a pipeline;
* two ERs asking during the same run take the terminal in turn rather than
  racing two reads on one stdin;
* the blocking read happens on a thread nobody has to join, so a run torn down
  with a question on screen exits instead of waiting out the prompt.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from finecode.cli_app.commands import run_cmd


async def test_a_question_with_no_options_is_not_a_refusal() -> None:
    """ "declined" means a person refused, and handlers honour that."""
    prompt_idle = asyncio.Event()
    prompt_idle.set()
    handler = run_cmd._make_elicit_handler(prompt_idle)

    assert await handler({"message": "?", "options": []}) == {"outcome": "unavailable"}


def test_the_prompt_itself_goes_to_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """`input(prompt)` would write the prompt to stdout; the run's output lives there."""
    monkeypatch.setattr("builtins.input", lambda: "2")

    result = run_cmd._ask_in_terminal("Keep them?", ["keep", "revert"], "keep")

    captured = capsys.readouterr()
    assert result == {"outcome": "answered", "value": "revert"}
    assert captured.out == ""
    assert "Choose 1-2 [keep]:" in captured.err
    assert "Keep them?" in captured.err


def test_end_of_input_declines_the_question(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    def _eof() -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)

    assert run_cmd._ask_in_terminal("?", ["yes", "no"], None) == {"outcome": "declined"}
    assert capsys.readouterr().out == ""


async def test_two_questions_take_the_terminal_in_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peak: list[int] = []
    live = 0
    guard = threading.Lock()

    def _fake_ask(_message: str, options: list[str], _default: str | None) -> dict:
        nonlocal live
        with guard:
            live += 1
            peak.append(live)
        time.sleep(0.05)
        with guard:
            live -= 1
        return {"outcome": "answered", "value": options[0]}

    monkeypatch.setattr(run_cmd, "_ask_in_terminal", _fake_ask)
    prompt_idle = asyncio.Event()
    prompt_idle.set()
    handler = run_cmd._make_elicit_handler(prompt_idle)

    answers = await asyncio.gather(
        handler({"message": "a?", "options": ["a"]}),
        handler({"message": "b?", "options": ["b"]}),
    )

    assert max(peak) == 1, "two prompts were on the same stdin at once"
    assert {answer["value"] for answer in answers} == {"a", "b"}
    assert prompt_idle.is_set()


async def test_output_is_held_back_while_a_question_is_on_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _fake_ask(_message: str, options: list[str], _default: str | None) -> dict:
        loop.call_soon_threadsafe(asked.set)
        time.sleep(0.05)
        return {"outcome": "answered", "value": options[0]}

    monkeypatch.setattr(run_cmd, "_ask_in_terminal", _fake_ask)
    prompt_idle = asyncio.Event()
    prompt_idle.set()
    handler = run_cmd._make_elicit_handler(prompt_idle)

    asking = asyncio.create_task(handler({"message": "?", "options": ["a"]}))
    await asyncio.wait_for(asked.wait(), timeout=2.0)
    assert not prompt_idle.is_set(), "streamed output would print into the prompt"

    await asyncio.wait_for(asking, timeout=2.0)
    assert prompt_idle.is_set()


async def test_the_prompt_runs_where_shutdown_never_joins_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run torn down mid-question must not wait out `input()` to exit.

    `asyncio.to_thread` borrows the default executor, whose threads
    `asyncio.run` joins on the way out — so a prompt nobody is going to answer
    would hold the process open. A daemon thread is abandoned instead.
    """
    seen: dict[str, object] = {}

    def _fake_ask(_message: str, options: list[str], _default: str | None) -> dict:
        thread = threading.current_thread()
        seen["daemon"] = thread.daemon
        return {"outcome": "answered", "value": options[0]}

    monkeypatch.setattr(run_cmd, "_ask_in_terminal", _fake_ask)

    await run_cmd._ask_off_loop("?", ["a"], None)

    assert seen["daemon"] is True


async def test_a_cancelled_question_lets_go_of_the_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The connection dropping cancels the handler; the abandoned thread may finish."""
    started = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _fake_ask(_message: str, options: list[str], _default: str | None) -> dict:
        loop.call_soon_threadsafe(started.set)
        time.sleep(0.1)
        return {"outcome": "answered", "value": options[0]}

    monkeypatch.setattr(run_cmd, "_ask_in_terminal", _fake_ask)
    prompt_idle = asyncio.Event()
    prompt_idle.set()
    handler = run_cmd._make_elicit_handler(prompt_idle)

    asking = asyncio.create_task(handler({"message": "?", "options": ["a"]}))
    await asyncio.wait_for(started.wait(), timeout=2.0)
    asking.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asking, timeout=1.0)
    # The late answer from the abandoned thread lands on a future nobody holds.
    await asyncio.sleep(0.15)
    assert prompt_idle.is_set()
