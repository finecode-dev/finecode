"""The CLI's side of ``client/elicit``: one terminal, questions that can end.

The prompt shares a process with a run that is streaming its results. Several
properties follow, and none of them shows up on the happy path:

* the question is written to stderr like everything else about the interaction,
  so ``finecode run ... > report.txt`` from a terminal produces the same bytes
  as one from a pipeline;
* two ERs asking during the same run take the terminal in turn;
* a question the WM stopped waiting for leaves the screen at once and says why,
  and what the person types afterwards reaches the next question, never the one
  that ended;
* a run torn down with a question on screen exits instead of waiting for input.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import sys
import threading
import typing

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.input.base import PipeInput
from prompt_toolkit.output import DummyOutput, Output
from prompt_toolkit.output.vt100 import Vt100_Output

from finecode import wm_client
from finecode.cli_app.terminal_questions import TerminalQuestions

ENDED = wm_client.RequestEndedWithoutAnswer


class _Terminal:
    def __init__(
        self,
        questions: TerminalQuestions,
        pipe: PipeInput,
        idle: asyncio.Event,
        discards: list[None],
        interrupts: list[None],
    ) -> None:
        self.questions = questions
        self.pipe = pipe
        self.idle = idle
        self.discards = discards
        self.interrupts = interrupts

    def ask(
        self,
        request_id: int,
        message: str = "Keep them?",
        options: list[str] | None = None,
        **extra: typing.Any,
    ) -> asyncio.Task[dict]:
        params = {"message": message, "options": options or ["keep", "revert"], **extra}
        return asyncio.create_task(self.questions.ask(request_id, params))

    def type(self, text: str) -> None:
        self.pipe.send_text(text)

    async def shown(self, count: int) -> None:
        """Wait until *count* questions have been put on screen."""
        await _until(lambda: len(self.discards) >= count and not self.idle.is_set())


async def _until(condition: typing.Callable[[], bool], timeout: float = 2.0) -> None:
    async def _poll() -> None:
        while not condition():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout=timeout)


@contextlib.contextmanager
def _terminal(
    *,
    output: Output | None = None,
    margin: float = 5.0,
    drain_pipe: bool = False,
) -> typing.Iterator[_Terminal]:
    with create_pipe_input() as pipe:
        idle = asyncio.Event()
        idle.set()
        discards: list[None] = []
        interrupts: list[None] = []

        def _discard() -> None:
            discards.append(None)
            if drain_pipe:
                fd = pipe.fileno()
                os.set_blocking(fd, False)
                with contextlib.suppress(BlockingIOError):
                    os.read(fd, 65536)

        questions = TerminalQuestions(
            prompt_idle=idle,
            prompt_input=pipe,
            prompt_output=output or DummyOutput(),
            discard_pending_input=_discard,
            on_interrupt=lambda: interrupts.append(None),
            fallback_margin_sec=margin,
        )
        yield _Terminal(questions, pipe, idle, discards, interrupts)


# ---------------------------------------------------------------------------
# What the person sees and how they answer
# ---------------------------------------------------------------------------


async def test_a_question_with_no_options_is_not_a_refusal() -> None:
    """ "declined" means a person refused, and handlers honour that."""
    with _terminal() as terminal:
        result = await terminal.questions.ask(1, {"message": "?", "options": []})

    assert result == {"outcome": "unavailable"}


async def test_the_question_and_the_prompt_go_to_stderr(
    capsys: pytest.CaptureFixture,
) -> None:
    """The run's output lives on stdout; the interaction must not touch it."""
    prompt_text = io.StringIO()
    output = Vt100_Output(prompt_text, lambda: Size(rows=24, columns=80), term="xterm")
    with _terminal(output=output) as terminal:
        asking = terminal.ask(1, default="keep")
        await terminal.shown(1)
        terminal.type("2\n")

        assert await asking == {"outcome": "answered", "value": "revert"}

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Keep them?" in captured.err
    assert "1) keep (default)" in captured.err
    assert "Choose 1-2 [keep]:" in prompt_text.getvalue()


async def test_an_option_can_be_typed_by_name_and_the_default_by_enter() -> None:
    with _terminal() as terminal:
        first = terminal.ask(1, default="keep")
        await terminal.shown(1)
        terminal.type("revert\n")
        assert await first == {"outcome": "answered", "value": "revert"}

        second = terminal.ask(2, default="keep")
        await terminal.shown(2)
        terminal.type("\n")
        assert await second == {"outcome": "answered", "value": "keep"}


async def test_an_invalid_answer_asks_again(capsys: pytest.CaptureFixture) -> None:
    with _terminal() as terminal:
        asking = terminal.ask(1)
        await terminal.shown(1)
        terminal.type("nonsense\n")
        seen: list[str] = []

        def _complained() -> bool:
            seen.append(capsys.readouterr().err)
            return "Not one of the options. Try again." in "".join(seen)

        await _until(_complained)
        terminal.type("1\n")

        assert await asking == {"outcome": "answered", "value": "keep"}


async def test_end_of_input_declines_the_question() -> None:
    with _terminal() as terminal:
        asking = terminal.ask(1)
        await terminal.shown(1)
        terminal.pipe.send_bytes(b"\x04")

        assert await asking == {"outcome": "declined"}
        await _until(terminal.idle.is_set)


async def test_two_questions_take_the_terminal_in_turn(
    capsys: pytest.CaptureFixture,
) -> None:
    with _terminal() as terminal:
        first = terminal.ask(1, "first?", ["a"])
        second = terminal.ask(2, "second?", ["b"])
        await terminal.shown(1)

        err = capsys.readouterr().err
        assert "first?" in err and "second?" not in err
        terminal.type("1\n")
        assert await first == {"outcome": "answered", "value": "a"}

        await terminal.shown(2)
        assert "second?" in capsys.readouterr().err
        terminal.type("1\n")
        assert await second == {"outcome": "answered", "value": "b"}
        await _until(terminal.idle.is_set)


async def test_output_is_held_back_while_a_question_is_on_screen() -> None:
    with _terminal() as terminal:
        asking = terminal.ask(1)
        await terminal.shown(1)
        assert not terminal.idle.is_set(), "streamed output would print into the prompt"

        terminal.type("1\n")
        await asking
        await _until(terminal.idle.is_set)


async def test_a_run_torn_down_mid_question_exits_at_once() -> None:
    """Nobody joins a thread: the prompt is a task, so cancelling it ends it."""
    threads_before = threading.active_count()
    with _terminal() as terminal:
        asking = terminal.ask(1)
        await terminal.shown(1)

        asking.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asking, timeout=1.0)
        await _until(terminal.idle.is_set, timeout=1.0)

    assert threading.active_count() == threads_before


async def test_a_full_size_draft_reaches_stderr_intact(
    capsys: pytest.CaptureFixture,
) -> None:
    """A complete 8192-byte draft is shown untruncated; stdout stays empty."""
    prefix = "FIRST-MARKER Add the thing\n\n"
    suffix = "\nLAST-MARKER\n"
    filler_len = 8192 - len(prefix.encode("utf-8")) - len(suffix.encode("utf-8"))
    draft = prefix + "x" * filler_len + suffix
    assert len(draft.encode("utf-8")) == 8192
    message = (
        "Commit the staged changes with the message below? "
        "Answer 'commit' to commit or 'cancel' to stop without committing.\n\n" + draft
    )
    options = ["commit", "cancel"]
    with _terminal() as terminal:
        accepted = terminal.ask(1, message, options, default="cancel")
        await terminal.shown(1)
        terminal.type("commit\n")
        assert await accepted == {"outcome": "answered", "value": "commit"}
        captured = capsys.readouterr()
        assert captured.out == ""
        assert draft in captured.err

        defaulted = terminal.ask(2, message, options, default="cancel")
        await terminal.shown(2)
        terminal.type("\n")
        assert await defaulted == {"outcome": "answered", "value": "cancel"}
        assert draft in capsys.readouterr().err

        declined = terminal.ask(3, message, options, default="cancel")
        await terminal.shown(3)
        terminal.pipe.send_bytes(b"\x04")
        assert await declined == {"outcome": "declined"}
        assert draft in capsys.readouterr().err


async def test_a_prompt_that_cannot_be_created_is_nobody_to_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _broken(*_args: object, **_kwargs: object) -> Output:
        raise OSError("no usable terminal")

    monkeypatch.setattr("prompt_toolkit.output.create_output", _broken)
    with create_pipe_input() as pipe:
        idle = asyncio.Event()
        idle.set()
        questions = TerminalQuestions(
            prompt_idle=idle,
            prompt_input=pipe,
            discard_pending_input=lambda: None,
            on_interrupt=lambda: None,
        )

        result = await asyncio.wait_for(
            questions.ask(1, {"message": "?", "options": ["a"]}), timeout=1.0
        )

    assert result == {"outcome": "unavailable"}
    assert idle.is_set()


async def test_ctrl_c_interrupts_the_run_and_frees_the_terminal() -> None:
    with _terminal() as terminal:
        asking = terminal.ask(1)
        await terminal.shown(1)
        terminal.pipe.send_bytes(b"\x03")

        with pytest.raises(ENDED):
            await asyncio.wait_for(asking, timeout=1.0)

        assert len(terminal.interrupts) == 1
        await _until(terminal.idle.is_set)


# ---------------------------------------------------------------------------
# A question that ends without an answer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "line"),
    [
        ("expired", "Question expired; no answer was sent."),
        ("runCancelled", "Question withdrawn: the run was cancelled."),
        (
            "noLongerNeeded",
            "Question withdrawn: the action no longer needs an answer.",
        ),
        ("some-future-reason", "Question withdrawn."),
    ],
)
async def test_a_withdrawn_question_says_why(
    reason: str, line: str, capsys: pytest.CaptureFixture
) -> None:
    with _terminal() as terminal:
        asking = terminal.ask(1)
        await terminal.shown(1)
        capsys.readouterr()

        await terminal.questions.on_withdrawn({"id": 1, "reason": reason})

        with pytest.raises(ENDED):
            await asyncio.wait_for(asking, timeout=1.0)
        await _until(terminal.idle.is_set)

    captured = capsys.readouterr()
    assert captured.err.strip() == line
    assert captured.out == ""


async def test_input_typed_after_a_withdrawal_reaches_the_next_question() -> None:
    """The first question's answer must never land on the second."""
    with _terminal() as terminal:
        first = terminal.ask(1, "first?")
        second = terminal.ask(2, "second?")
        await terminal.shown(1)

        await terminal.questions.on_withdrawn({"id": 1, "reason": "expired"})
        with pytest.raises(ENDED):
            await first
        await terminal.shown(2)
        terminal.type("2\n")

        assert await second == {"outcome": "answered", "value": "revert"}
        assert len(terminal.discards) == 2, (
            "stray input is dropped before each question"
        )


@pytest.mark.skipif(sys.platform == "win32", reason="drains a POSIX pipe descriptor")
async def test_a_line_typed_while_no_question_is_shown_answers_nothing() -> None:
    with _terminal(drain_pipe=True) as terminal:
        terminal.type("2\n")
        await asyncio.sleep(0.02)

        asking = terminal.ask(1)
        await terminal.shown(1)
        await asyncio.sleep(0.1)
        assert not asking.done()

        terminal.type("1\n")
        assert await asking == {"outcome": "answered", "value": "keep"}


async def test_a_question_withdrawn_while_waiting_is_never_shown(
    capsys: pytest.CaptureFixture,
) -> None:
    with _terminal() as terminal:
        first = terminal.ask(1, "first?")
        second = terminal.ask(2, "second?")
        await terminal.shown(1)

        await terminal.questions.on_withdrawn({"id": 2, "reason": "expired"})
        terminal.type("1\n")
        await first
        with pytest.raises(ENDED):
            await second
        await _until(terminal.idle.is_set)

    captured = capsys.readouterr()
    assert "second?" not in captured.err
    assert "expired" not in captured.err
    assert len(terminal.discards) == 1


async def test_a_withdrawal_for_an_unknown_question_is_ignored() -> None:
    with _terminal() as terminal:
        asking = terminal.ask(1)
        await terminal.shown(1)

        await terminal.questions.on_withdrawn({"id": 99, "reason": "expired"})
        await terminal.questions.on_withdrawn(None)

        assert not asking.done()
        terminal.type("1\n")
        assert await asking == {"outcome": "answered", "value": "keep"}


async def test_connection_loss_ends_shown_and_waiting_questions(
    capsys: pytest.CaptureFixture,
) -> None:
    with _terminal() as terminal:
        shown = terminal.ask(1, "first?")
        waiting = terminal.ask(2, "second?")
        await terminal.shown(1)
        capsys.readouterr()

        terminal.questions.end_all_on_connection_lost()

        for asking in (shown, waiting):
            with pytest.raises(ENDED):
                await asyncio.wait_for(asking, timeout=1.0)
        await _until(terminal.idle.is_set)

    captured = capsys.readouterr()
    assert captured.err.strip() == (
        "Question withdrawn: the connection to the FineCode WM was lost."
    )
    assert "second?" not in captured.err


# ---------------------------------------------------------------------------
# A WM that never withdraws
# ---------------------------------------------------------------------------


async def test_a_held_question_expires_after_its_deadline_plus_the_margin(
    capsys: pytest.CaptureFixture,
) -> None:
    """Real time on purpose: the prompt reads a real pipe, so virtual time cannot drive it."""
    with _terminal(margin=0.15) as terminal:
        loop = asyncio.get_running_loop()
        started = loop.time()
        asking = terminal.ask(1, timeoutSec=0.1)
        await terminal.shown(1)

        with pytest.raises(ENDED):
            await asyncio.wait_for(asking, timeout=2.0)
        elapsed = loop.time() - started
        await _until(terminal.idle.is_set)

    assert elapsed >= 0.25
    assert "Question expired; no answer was sent." in capsys.readouterr().err


async def test_a_waiting_question_is_dropped_by_its_own_timer(
    capsys: pytest.CaptureFixture,
) -> None:
    with _terminal(margin=0.05) as terminal:
        first = terminal.ask(1, "first?")
        second = terminal.ask(2, "second?", timeoutSec=0.05)
        await terminal.shown(1)

        with pytest.raises(ENDED):
            await asyncio.wait_for(second, timeout=2.0)
        terminal.type("1\n")
        await first

    assert "second?" not in capsys.readouterr().err


async def test_a_withdrawal_cancels_the_local_timer(
    capsys: pytest.CaptureFixture,
) -> None:
    with _terminal(margin=0.05) as terminal:
        asking = terminal.ask(1, timeoutSec=0.05)
        await terminal.shown(1)
        capsys.readouterr()

        await terminal.questions.on_withdrawn({"id": 1, "reason": "noLongerNeeded"})
        with pytest.raises(ENDED):
            await asking
        await asyncio.sleep(0.2)

    assert capsys.readouterr().err.count("Question") == 1


async def test_a_question_without_a_deadline_never_expires_locally() -> None:
    with _terminal(margin=0.01) as terminal:
        asking = terminal.ask(1)
        await terminal.shown(1)
        await asyncio.sleep(0.1)

        assert not asking.done()
        terminal.type("1\n")
        await asking


# ---------------------------------------------------------------------------
# Through a real client reader
# ---------------------------------------------------------------------------


async def test_a_question_withdrawn_right_behind_its_request_is_never_shown(
    capsys: pytest.CaptureFixture,
) -> None:
    """The request and its withdrawal can arrive in one read; the order must hold."""
    import json

    received: list[dict] = []
    writer_holder: list[asyncio.StreamWriter] = []
    connected = asyncio.Event()

    async def _serve(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        writer_holder.append(writer)
        connected.set()
        try:
            while True:
                header = await reader.readline()
                if not header:
                    return
                length = int(header.decode().split(":")[1].strip())
                await reader.readline()
                body = json.loads((await reader.readexactly(length)).decode())
                received.append(body)
                if "method" in body and "id" in body:
                    _write(writer, {"jsonrpc": "2.0", "id": body["id"], "result": {}})
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return

    def _frame(msg: dict) -> bytes:
        body = json.dumps(msg).encode()
        return f"Content-Length: {len(body)}\r\n\r\n".encode() + body

    def _write(writer: asyncio.StreamWriter, msg: dict) -> None:
        writer.write(_frame(msg))

    server = await asyncio.start_server(_serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = wm_client.ApiClient()
    try:
        with _terminal() as terminal:
            client.on_request(
                "client/elicit", terminal.questions.ask, pass_request_id=True
            )
            client.on_notification(
                "client/elicitWithdrawn", terminal.questions.on_withdrawn
            )
            await client.connect("127.0.0.1", port)
            writer = writer_holder[0]

            def _question(request_id: int, message: str) -> dict:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "client/elicit",
                    "params": {"message": message, "options": ["a", "b"]},
                }

            writer.write(_frame(_question(1, "first?")))
            await terminal.shown(1)
            writer.write(
                _frame(_question(2, "second?"))
                + _frame(
                    {
                        "jsonrpc": "2.0",
                        "method": "client/elicitWithdrawn",
                        "params": {"id": 2, "reason": "expired"},
                    }
                )
            )
            await asyncio.sleep(0.1)
            terminal.type("1\n")
            await _until(
                lambda: any(m.get("id") == 1 and "method" not in m for m in received)
            )
            await _until(terminal.idle.is_set)
            await asyncio.sleep(0.05)

            assert "second?" not in capsys.readouterr().err
            assert not any(m.get("id") == 2 and "method" not in m for m in received), (
                "a withdrawn question is not answered"
            )
    finally:
        await client.close()
        for writer in writer_holder:
            writer.close()
        server.close()
        await server.wait_closed()
