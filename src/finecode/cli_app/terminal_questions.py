"""Closed-choice questions put to the person at the terminal (ADR-0082, ADR-0167).

The WM sends ``client/elicit`` and may later send ``client/elicitWithdrawn`` for
the same id when it stops waiting for the answer. Two things go wrong if a
terminal client only reads a line: a question the WM already gave up on stays on
screen, and a line typed for it is consumed by whichever question comes next.
This module holds every question as soon as it is read, shows one at a time,
ends a shown question the moment it is withdrawn, expires or loses its
connection, says why on stderr, and discards stray input before showing the
next one.

Everything is written to stderr, including the prompt: the answer belongs to the
interaction, not to the run's output.
"""

from __future__ import annotations

import asyncio
import collections.abc
import dataclasses
import re
import sys
import typing

import click
import prompt_toolkit
import prompt_toolkit.input
import prompt_toolkit.output
from loguru import logger

from finecode.wm_client import RequestEndedWithoutAnswer
from finecode.wm_server.runner import elicitation_bridge

if sys.platform == "win32":
    import msvcrt
else:
    import termios

__all__ = ["TerminalQuestions", "discard_pending_terminal_input"]

_EXPIRED_NOTICE = "Question expired; no answer was sent."
_UNKNOWN_REASON_NOTICE = "Question withdrawn."
_CONNECTION_LOST_NOTICE = (
    "Question withdrawn: the connection to the FineCode WM was lost."
)
_NOTICE_BY_REASON = {
    "expired": _EXPIRED_NOTICE,
    "runCancelled": "Question withdrawn: the run was cancelled.",
    "noLongerNeeded": "Question withdrawn: the action no longer needs an answer.",
}


_TEXT_HINT_SINGLE = "(Enter submits; Ctrl-D on an empty line declines)"
_TEXT_HINT_MULTI = "(Esc then Enter submits; Ctrl-D on an empty answer declines)"
_TEXT_PROMPT = "> "
_TOO_LONG_NOTICE = "The answer is longer than 65536 characters; no answer was sent."
_UNDECODABLE_NOTICE = "The answer is not valid UTF-8 text; no answer was sent."
_MULTI_INVALID_NOTICE = "Not a list of option numbers. Try again."


def _escape_message(text: str) -> str:
    """Escape controls in a question message for terminal display."""
    out: list[str] = []
    for char in text:
        code = ord(char)
        if char in ("\n", "\t"):
            out.append(char)
            continue
        if code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F:
            out.append(char.encode("unicode_escape").decode("ascii"))
        else:
            out.append(char)
    return "".join(out)


def _escape_inline(text: str) -> str:
    """Escape every control character for single-line display."""
    out: list[str] = []
    for char in text:
        code = ord(char)
        if code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F:
            out.append(char.encode("unicode_escape").decode("ascii"))
        else:
            out.append(char)
    return "".join(out)


def _has_lone_surrogate(text: str) -> bool:
    return any(0xD800 <= ord(char) <= 0xDFFF for char in text)


def discard_pending_terminal_input() -> None:
    """Drop whatever the person typed that no question has read yet."""
    if sys.platform == "win32":
        while msvcrt.kbhit():
            msvcrt.getwch()
    else:
        try:
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        except (termios.error, ValueError, OSError):
            logger.debug("Could not discard pending terminal input")


@dataclasses.dataclass
class _HeldQuestion:
    request_id: int | str
    kind: str
    message: str
    options: list[str]
    default: str | None
    outcome: asyncio.Future[dict[str, typing.Any]]
    shown: bool = False
    # What to tell the person if the question ends while it is on screen.
    notice: str | None = None
    prompt_task: asyncio.Task[None] | None = None
    expiry: asyncio.TimerHandle | None = None
    multiline: bool = False
    initial_value: str | None = None
    placeholder: str | None = None
    descriptions: dict[str, str] = dataclasses.field(default_factory=dict)
    preselected: list[str] = dataclasses.field(default_factory=list)


class TerminalQuestions:
    """Holds, shows and ends the questions asked of this terminal."""

    def __init__(
        self,
        *,
        prompt_idle: asyncio.Event,
        discard_pending_input: collections.abc.Callable[[], None],
        on_interrupt: collections.abc.Callable[[], None],
        prompt_input: prompt_toolkit.input.Input | None = None,
        prompt_output: prompt_toolkit.output.Output | None = None,
        fallback_margin_sec: float = 5.0,
    ) -> None:
        self._prompt_idle = prompt_idle
        self._discard_pending_input = discard_pending_input
        self._on_interrupt = on_interrupt
        self._prompt_input = prompt_input
        self._prompt_output = prompt_output
        self._fallback_margin_sec = fallback_margin_sec
        self._held: dict[int | str, _HeldQuestion] = {}
        self._queue: list[_HeldQuestion] = []
        self._pump: asyncio.Task[None] | None = None

    async def ask(self, request_id: int | str, params: dict | None) -> dict:
        """Serve ``client/elicit``.

        Raises:
            RequestEndedWithoutAnswer: the question was withdrawn, expired or
                lost its connection, so the WM is no longer waiting for an answer.
        """
        value = params or {}
        kind = value.get("kind", "choice")
        if kind not in ("choice", "text", "multiChoice"):
            return {"outcome": "unavailable"}
        options = [str(option) for option in value.get("options") or []]
        if kind in ("choice", "multiChoice") and not options:
            # Nobody can be asked a question with no answers. Not "declined":
            # that means a person refused, which handlers are documented to
            # honour by aborting or taking the safe branch.
            return {"outcome": "unavailable"}

        message = str(value.get("message", ""))
        descriptions = {
            str(key): str(desc)
            for key, desc in (value.get("descriptions") or {}).items()
            if str(key) in options
        }
        if kind == "choice":
            default = value.get("default")
            if default not in options:
                default = None
            held_kwargs: dict[str, object] = {"default": default}
        elif kind == "text":
            held_kwargs = {
                "default": None,
                "multiline": bool(value.get("multiline", False)),
                "initial_value": value.get("initialValue"),
                "placeholder": value.get("placeholder"),
            }
        else:
            raw_default = value.get("default") or []
            if not isinstance(raw_default, list):
                raw_default = []
            preselected = [str(e) for e in raw_default if str(e) in options]
            held_kwargs = {"default": None, "preselected": preselected}

        loop = asyncio.get_running_loop()
        held = _HeldQuestion(
            request_id=request_id,
            kind=str(kind),
            message=message,
            options=options,
            outcome=loop.create_future(),
            descriptions=descriptions,
            **held_kwargs,
        )
        # Held before the first suspension: a withdrawal read right behind this
        # request finds the question and drops it before it is ever shown.
        self._held[request_id] = held
        self._queue.append(held)
        timeout_sec = value.get("timeoutSec")
        if isinstance(timeout_sec, (int, float)):
            # The WM owns the deadline and normally withdraws first; this only
            # covers a WM that never does, and sends nothing when it fires.
            held.expiry = loop.call_later(
                timeout_sec + self._fallback_margin_sec,
                self._end,
                held,
                _EXPIRED_NOTICE,
            )
        self._ensure_pump()
        try:
            return await held.outcome
        finally:
            self._finish(held)

    async def on_withdrawn(
        self, params: collections.abc.Mapping[str, typing.Any] | None
    ) -> None:
        """Serve ``client/elicitWithdrawn``: the WM stopped waiting for a question.

        ``reason`` is read as an open string: a later WM may name causes this
        client does not know, and those still end the question.
        """
        if not params:
            return
        held = self._held.get(params["id"])
        if held is None:
            return
        reason = str(params.get("reason"))
        self._end(held, _NOTICE_BY_REASON.get(reason, _UNKNOWN_REASON_NOTICE))

    def end_all_on_connection_lost(self) -> None:
        """End every held question, shown or waiting, because the WM is gone."""
        for held in list(self._held.values()):
            self._end(held, _CONNECTION_LOST_NOTICE)

    def _end(self, held: _HeldQuestion, notice: str | None) -> None:
        if held.outcome.done():
            return
        held.notice = notice
        held.outcome.set_exception(RequestEndedWithoutAnswer())
        self._finish(held)

    def _finish(self, held: _HeldQuestion) -> None:
        """Release what a question holds; safe to repeat."""
        if held.expiry is not None:
            held.expiry.cancel()
            held.expiry = None
        self._held.pop(held.request_id, None)
        prompt_task = held.prompt_task
        if prompt_task is not None and prompt_task is not asyncio.current_task():
            prompt_task.cancel()

    def _ensure_pump(self) -> None:
        if self._pump is None or self._pump.done():
            self._pump = asyncio.create_task(self._run_pump())

    async def _run_pump(self) -> None:
        while self._queue:
            held = self._queue.pop(0)
            if held.outcome.done():
                continue
            try:
                await self._show(held)
            except Exception:
                logger.exception("Showing a question at the terminal failed")
                if not held.outcome.done():
                    held.outcome.set_result({"outcome": "unavailable"})
                self._finish(held)

    async def _show(self, held: _HeldQuestion) -> None:
        self._prompt_idle.clear()
        try:
            try:
                session = prompt_toolkit.PromptSession(
                    input=self._prompt_input,
                    output=self._prompt_output
                    or prompt_toolkit.output.create_output(stdout=sys.stderr),
                )
            except Exception as error:
                # The terminal cannot be driven: nobody could be asked, which is
                # an outcome the handler already knows how to take (ADR-0082
                # rule 3), not a reason to crash the run.
                logger.debug(f"Cannot prompt at this terminal: {error}")
                self._settle(held, {"outcome": "unavailable"})
                return

            self._discard_pending_input()
            held.shown = True
            click.echo("", err=True)
            click.echo(click.style(_escape_message(held.message), bold=True), err=True)
            if held.kind == "choice":
                for index, option in enumerate(held.options, start=1):
                    marker = " (default)" if option == held.default else ""
                    desc = held.descriptions.get(option)
                    suffix = (
                        f" \u2014 {_escape_inline(desc)}" if desc is not None else ""
                    )
                    click.echo(
                        f"  {index}) {_escape_inline(option)}{marker}{suffix}",
                        err=True,
                    )
            elif held.kind == "text":
                if held.placeholder is not None:
                    click.echo(f"Example: {_escape_inline(held.placeholder)}", err=True)
                hint = _TEXT_HINT_MULTI if held.multiline else _TEXT_HINT_SINGLE
                click.echo(hint, err=True)
            else:
                for index, option in enumerate(held.options, start=1):
                    checked = "[x]" if option in held.preselected else "[ ]"
                    desc = held.descriptions.get(option)
                    suffix = (
                        f" \u2014 {_escape_inline(desc)}" if desc is not None else ""
                    )
                    click.echo(
                        f"  {index}) {checked} {_escape_inline(option)}{suffix}",
                        err=True,
                    )

            held.prompt_task = asyncio.create_task(self._read_answer(held, session))
            try:
                await asyncio.wait([held.prompt_task])
            finally:
                held.prompt_task.cancel()
            if not held.prompt_task.cancelled():
                error = held.prompt_task.exception()
                if error is not None and not held.outcome.done():
                    held.outcome.set_exception(error)
            held.prompt_task = None
            if held.notice is not None:
                click.echo(held.notice, err=True)
        finally:
            self._prompt_idle.set()

    async def _read_answer(
        self, held: _HeldQuestion, session: prompt_toolkit.PromptSession
    ) -> None:
        if held.kind == "text":
            await self._read_text_answer(held, session)
        elif held.kind == "multiChoice":
            await self._read_multi_answer(held, session)
        else:
            await self._read_choice_answer(held, session)

    async def _read_choice_answer(
        self, held: _HeldQuestion, session: prompt_toolkit.PromptSession
    ) -> None:
        has_default = held.default in held.options
        hint = f" [{held.default}]" if has_default else ""
        prompt = f"Choose 1-{len(held.options)}{hint}: "
        while True:
            try:
                answer = (await session.prompt_async(prompt)).strip()
            except EOFError:
                # Ctrl-D ends *the question*, not the run: the person was asked
                # and did not choose, which is a decision the handler can act on.
                self._settle(held, {"outcome": "declined"})
                return
            except KeyboardInterrupt:
                # Ctrl-C ends the run, as it does everywhere else in this CLI.
                self._on_interrupt()
                self._end(held, None)
                return

            if not answer and has_default:
                # Enter on a defaulted question is a person accepting the
                # default, which is an answer — unlike the handler applying it
                # unasked.
                self._settle(held, {"outcome": "answered", "value": held.default})
                return
            if answer.isdigit() and 1 <= int(answer) <= len(held.options):
                self._settle(
                    held,
                    {"outcome": "answered", "value": held.options[int(answer) - 1]},
                )
                return
            if answer in held.options:
                self._settle(held, {"outcome": "answered", "value": answer})
                return
            click.echo("Not one of the options. Try again.", err=True)

    async def _read_text_answer(
        self, held: _HeldQuestion, session: prompt_toolkit.PromptSession
    ) -> None:
        while True:
            try:
                answer = await session.prompt_async(
                    _TEXT_PROMPT,
                    multiline=held.multiline,
                    default=held.initial_value or "",
                )
            except EOFError:
                self._settle(held, {"outcome": "declined"})
                return
            except KeyboardInterrupt:
                self._on_interrupt()
                self._end(held, None)
                return
            if _has_lone_surrogate(answer):
                click.echo(_UNDECODABLE_NOTICE, err=True)
                self._settle(held, {"outcome": "unavailable"})
                return
            if len(answer) > elicitation_bridge.QUESTION_TEXT_MAX_CHARS:
                click.echo(_TOO_LONG_NOTICE, err=True)
                self._settle(held, {"outcome": "unavailable"})
                return
            self._settle(held, {"outcome": "answered", "value": answer})
            return

    async def _read_multi_answer(
        self, held: _HeldQuestion, session: prompt_toolkit.PromptSession
    ) -> None:
        numbers = [
            str(index + 1)
            for index, opt in enumerate(held.options)
            if opt in held.preselected
        ]
        keeps = f" [Enter keeps {' '.join(numbers)}]" if numbers else ""
        prompt = (
            f"Choose any of 1-{len(held.options)}"
            " (spaces or commas; - for none)"
            f"{keeps}: "
        )
        while True:
            try:
                raw = await session.prompt_async(prompt)
            except EOFError:
                self._settle(held, {"outcome": "declined"})
                return
            except KeyboardInterrupt:
                self._on_interrupt()
                self._end(held, None)
                return
            stripped = raw.strip()
            if stripped == "":
                if held.preselected:
                    ordered = [
                        opt for opt in held.options if opt in set(held.preselected)
                    ]
                    self._settle(held, {"outcome": "answered", "values": ordered})
                else:
                    self._settle(held, {"outcome": "answered", "values": []})
                return
            if stripped == "-":
                self._settle(held, {"outcome": "answered", "values": []})
                return
            parts = [p for p in re.split(r"[\s,]+", stripped) if p]
            chosen: set[int] = set()
            valid = bool(parts)
            for part in parts:
                if not part.isdigit() or not 1 <= int(part) <= len(held.options):
                    valid = False
                    break
                chosen.add(int(part))
            if not valid:
                click.echo(_MULTI_INVALID_NOTICE, err=True)
                continue
            ordered = [
                opt for idx, opt in enumerate(held.options, start=1) if idx in chosen
            ]
            self._settle(held, {"outcome": "answered", "values": ordered})
            return

    def _settle(self, held: _HeldQuestion, result: dict[str, typing.Any]) -> None:
        if not held.outcome.done():
            held.outcome.set_result(result)
        self._finish(held)
