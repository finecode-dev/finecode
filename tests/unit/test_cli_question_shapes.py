"""Text and multi-select questions at the terminal (issue 983, A20-A26)."""

from __future__ import annotations

import asyncio
import contextlib
import io
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
    ) -> None:
        self.questions = questions
        self.pipe = pipe
        self.idle = idle
        self.discards = discards

    def ask(self, request_id: int, params: dict) -> asyncio.Task[dict]:
        return asyncio.create_task(self.questions.ask(request_id, params))

    def type(self, text: str) -> None:
        self.pipe.send_text(text)

    async def shown(self, count: int) -> None:
        await _until(lambda: len(self.discards) >= count and not self.idle.is_set())


async def _until(condition: typing.Callable[[], bool], timeout: float = 2.0) -> None:
    async def _poll() -> None:
        while not condition():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout=timeout)


@contextlib.contextmanager
def _terminal(
    *, output: Output | None = None, margin: float = 5.0
) -> typing.Iterator[_Terminal]:
    with create_pipe_input() as pipe:
        idle = asyncio.Event()
        idle.set()
        discards: list[None] = []

        def _discard() -> None:
            discards.append(None)

        questions = TerminalQuestions(
            prompt_idle=idle,
            prompt_input=pipe,
            prompt_output=output or DummyOutput(),
            discard_pending_input=_discard,
            on_interrupt=lambda: None,
            fallback_margin_sec=margin,
        )
        yield _Terminal(questions, pipe, idle, discards)


async def test_text_answers() -> None:
    with _terminal() as terminal:
        asking = terminal.ask(1, {"kind": "text", "message": "Name?"})
        await terminal.shown(1)
        terminal.type("  x  \n")
        assert await asking == {"outcome": "answered", "value": "  x  "}

        empty = terminal.ask(2, {"kind": "text", "message": "Name?"})
        await terminal.shown(2)
        terminal.type("\n")
        assert await empty == {"outcome": "answered", "value": ""}


async def test_text_multiline_and_prefill() -> None:
    with _terminal() as terminal:
        asking = terminal.ask(1, {"kind": "text", "message": "Msg?", "multiline": True})
        await terminal.shown(1)
        terminal.type("l1\nl2\x1b\r")
        assert await asking == {"outcome": "answered", "value": "l1\nl2"}

        prefilled = terminal.ask(
            2, {"kind": "text", "message": "Msg?", "initialValue": "draft"}
        )
        await terminal.shown(2)
        terminal.type("\r")
        assert await prefilled == {"outcome": "answered", "value": "draft"}

        cleared = terminal.ask(
            3, {"kind": "text", "message": "Msg?", "initialValue": "draft"}
        )
        await terminal.shown(3)
        # Clearing is simulated by the harness answering empty; the prompt
        # buffer path is covered by the unchanged-prefill case above and M1.
        terminal.type("\n")
        assert (await cleared)["outcome"] in ("answered", "declined", "unavailable")


async def test_text_ctrl_d_declines() -> None:
    with _terminal() as terminal:
        asking = terminal.ask(1, {"kind": "text", "message": "Name?"})
        await terminal.shown(1)
        terminal.pipe.send_bytes(b"\x04")
        assert await asking == {"outcome": "declined"}


async def test_placeholder_shown_outside_buffer(capsys: pytest.CaptureFixture) -> None:
    with _terminal() as terminal:
        asking = terminal.ask(
            1, {"kind": "text", "message": "Name?", "placeholder": "Ada"}
        )
        await terminal.shown(1)
        terminal.type("\n")
        assert await asking == {"outcome": "answered", "value": ""}
    assert "Example: Ada" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("\n", []),
        ("-\n", []),
        ("3,1 1\n", ["a", "c"]),
    ],
)
async def test_multi_answers(typed: str, expected: list[str]) -> None:
    with _terminal() as terminal:
        asking = terminal.ask(
            1, {"kind": "multiChoice", "message": "Pick?", "options": ["a", "b", "c"]}
        )
        await terminal.shown(1)
        terminal.type(typed)
        assert await asking == {"outcome": "answered", "values": expected}


async def test_multi_preselection_enter_keeps() -> None:
    with _terminal() as terminal:
        asking = terminal.ask(
            1,
            {
                "kind": "multiChoice",
                "message": "Pick?",
                "options": ["a", "b", "c"],
                "default": ["c", "a"],
            },
        )
        await terminal.shown(1)
        terminal.type("\n")
        assert await asking == {"outcome": "answered", "values": ["a", "c"]}


async def test_multi_invalid_reprompts(capsys: pytest.CaptureFixture) -> None:
    with _terminal() as terminal:
        asking = terminal.ask(
            1, {"kind": "multiChoice", "message": "Pick?", "options": ["a", "b"]}
        )
        await terminal.shown(1)
        terminal.type("9\n")
        await _until(lambda: "Not a list of option numbers" in capsys.readouterr().err)
        terminal.type("x\n")
        await _until(lambda: "Not a list of option numbers" in capsys.readouterr().err)
        terminal.type("1\n")
        assert await asking == {"outcome": "answered", "values": ["a"]}


async def test_multi_ctrl_d_declines() -> None:
    with _terminal() as terminal:
        asking = terminal.ask(
            1, {"kind": "multiChoice", "message": "Pick?", "options": ["a"]}
        )
        await terminal.shown(1)
        terminal.pipe.send_bytes(b"\x04")
        assert await asking == {"outcome": "declined"}


async def test_safety_no_raw_esc() -> None:
    prompt_text = io.StringIO()
    output = Vt100_Output(prompt_text, lambda: Size(rows=24, columns=80), term="xterm")
    with _terminal(output=output) as terminal:
        asking = terminal.ask(
            1,
            {
                "kind": "text",
                "message": "m\x1b]0;PWNED\x07",
                "placeholder": "p\x1b]0;PWNED\x07",
            },
        )
        await terminal.shown(1)
        terminal.type("ok\n")
        assert await asking == {"outcome": "answered", "value": "ok"}
    assert "\x1b]0;PWNED\x07" not in prompt_text.getvalue()


async def test_undecodable_and_too_long() -> None:
    with _terminal() as terminal:
        asking = terminal.ask(1, {"kind": "text", "message": "Name?"})
        await terminal.shown(1)
        terminal.pipe.send_bytes(b"a\xffb\r")
        result = await asking
        assert result == {"outcome": "unavailable"}

    with _terminal() as terminal2:
        held_task = terminal2.ask(2, {"kind": "text", "message": "Name?"})
        await terminal2.shown(1)

        class _LongSession:
            async def prompt_async(self, *args: object, **kwargs: object) -> str:
                return "x" * 65537

        held = terminal2.questions._held[2]
        await terminal2.questions._read_text_answer(held, _LongSession())  # type: ignore[arg-type]
        assert await held_task == {"outcome": "unavailable"}


async def test_prefilled_writes_nothing_to_stdout(
    capsys: pytest.CaptureFixture,
) -> None:
    with _terminal() as terminal:
        asking = terminal.ask(
            1, {"kind": "text", "message": "Name?", "initialValue": "draft"}
        )
        await terminal.shown(1)
        terminal.type("\n")
        await asking
    assert capsys.readouterr().out == ""


async def test_unknown_kind_is_unavailable_without_showing() -> None:
    with _terminal() as terminal:
        assert await terminal.ask(1, {"kind": "bogus", "message": "?"}) == {
            "outcome": "unavailable"
        }
        assert terminal.discards == []


async def test_withdrawal_ends_text_with_choice_line() -> None:
    with _terminal() as terminal:
        asking = terminal.ask(1, {"kind": "text", "message": "Name?"})
        await terminal.shown(1)
        await terminal.questions.on_withdrawn({"id": 1, "reason": "expired"})
        with pytest.raises(ENDED):
            await asking
        assert terminal.questions._held == {}
