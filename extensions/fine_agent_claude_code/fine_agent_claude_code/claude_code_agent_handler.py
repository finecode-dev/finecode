import asyncio
import contextlib
import dataclasses
import json
import shlex
import time

from fine_agent import backend_support
from fine_agent.run_agent_task_action import (
    AgentRunStatus,
    RunAgentTaskAction,
    RunAgentTaskRunContext,
    RunAgentTaskRunPayload,
    RunAgentTaskRunResult,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    icommandrunner,
    ilogger,
    iprojectinfoprovider,
    iuser_messenger,
)

from fine_agent_claude_code import claude_code_stream

_EXIT_GRACE_SEC = 5.0
"""How long to let claude exit once its stream has ended.

Short on purpose: by this point the run has already produced its answer or been
withdrawn, and the only thing still being waited for is the process noticing
that its input is closed.
"""

_SIGNAL_GRACE_SEC = 2.0
"""How long each signal in the teardown ladder gets before the next one.

Shorter than the exit grace: by this point claude has already outstayed a clean
exit, so what is being waited for is the kernel delivering a signal, not an
agent winding down.
"""


@dataclasses.dataclass
class ClaudeCodeAgentProfile:
    """Per-role overrides for one named agent run. A `None` field inherits the
    handler's top-level value."""

    model: str | None = None
    settle_timeout_sec: float | None = None


@dataclasses.dataclass
class ClaudeCodeAgentHandlerConfig(code_action.ActionHandlerConfig):
    model: str | None = None
    """Model passed to `claude --model`, as an alias (`opus`, `sonnet`) or a
    full name. `None` leaves the CLI's own default.

    Lives here rather than on the payload so the same task definition runs
    against a different model without being edited (PRD-0005 R3).
    """
    permission_mode: str | None = None
    """Passed to `claude --permission-mode`. `None` leaves the CLI's default,
    which refuses anything needing approval rather than guessing on the user's
    behalf -- the same fail-closed default `fine_agent_pi` takes.

    A task that must edit files therefore needs a mode that allows it
    (`acceptEdits`) or an explicit `allowed_tools`. That is deliberately a
    decision a setup has to make: this handler runs an agent with write access
    to the user's project, and the safe default is the one where nothing
    unapproved happens.
    """
    allowed_tools: list[str] = dataclasses.field(default_factory=list)
    """Tool patterns granted without asking, e.g. `["Read", "Bash(git *)"]`."""
    disallowed_tools: list[str] = dataclasses.field(default_factory=list)
    """Tool patterns denied outright, applied over `allowed_tools`."""
    append_system_prompt: str | None = None
    """Extra instructions appended to the CLI's own system prompt. Config
    rather than payload for the same reason `model` is: it shapes the setup an
    agent runs in, not the task it is given."""
    max_budget_usd: float | None = None
    """Ceiling on what one run may spend on API calls, enforced by the CLI."""
    settle_timeout_sec: float = 900.0
    """Ceiling on one agent run. An agent loop has no natural bound, so without
    this a wedged run holds an ER subprocess slot indefinitely."""
    profiles: dict[str, ClaudeCodeAgentProfile] = dataclasses.field(
        default_factory=dict
    )
    """Named runs, keyed by the `profile` a caller passes, so an override can be
    expressed in an environment variable (S-205).

    A field left `None` on a profile inherits the top-level value; a profile
    therefore cannot ask for the CLI's own default for a field the top level
    sets. `permission_mode`, `allowed_tools` and `max_budget_usd` stay top-level
    in this slice -- a future knob must choose inherit-or-empty deliberately,
    since inheriting a permission set can grant more than the profile intended.
    """


@dataclasses.dataclass(frozen=True)
class _ClaudeRunSettings:
    """The claude run knobs for one run, after a profile is resolved."""

    model: str | None
    settle_timeout_sec: float


class ClaudeCodeAgentHandler(
    code_action.ActionHandler[
        RunAgentTaskAction,
        ClaudeCodeAgentHandlerConfig,
    ]
):
    """Run a task with Claude Code, driving its non-interactive print mode.

    `claude -p --output-format stream-json` emits one JSON object per line for
    the run's whole duration and ends with a `result` frame, so this handler
    owns the pipe until that frame arrives and forwards what it sees into
    FineCode's progress surface.

    The prompt goes on stdin rather than in the command line: it is arbitrary
    user text of arbitrary length, and stdin has neither a length limit nor a
    quoting problem. Nothing is sent afterwards -- stdin closes immediately,
    which is also why there is no in-band way to abort a run once it starts
    (see `_abort`).

    Exit codes: non-zero is treated as failure with stderr attached, but `0` is
    *not* sufficient for success. The outcome is decided from the `result`
    frame; the exit code only adds to it.
    """

    def __init__(
        self,
        config: ClaudeCodeAgentHandlerConfig,
        logger: ilogger.ILogger,
        command_runner: icommandrunner.ICommandRunner,
        user_messenger: iuser_messenger.IUserMessenger,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
    ) -> None:
        self.config = config
        self.logger = logger
        self.command_runner = command_runner
        self.user_messenger = user_messenger
        self.project_info_provider = project_info_provider

    async def run(
        self,
        payload: RunAgentTaskRunPayload,
        run_context: RunAgentTaskRunContext,
    ) -> RunAgentTaskRunResult:
        settings = self._resolve(payload.profile)
        if isinstance(settings, str):
            # Fail before spawning anything: an unknown profile is a
            # configuration error, not a run that got partway.
            return RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED, error=settings, duration_sec=0.0
            )
        command = self._build_command(settings, payload.output_schema)
        project_dir = self.project_info_provider.get_current_project_dir_path()
        self.logger.debug(f"Starting claude: {command} in {project_dir}")

        started = time.monotonic()
        process = await self.command_runner.run(
            command,
            cwd=project_dir,
            # claude runs tools of its own, so tearing it down has to reach them
            # too -- otherwise an abandoned run leaves a subtree editing the
            # project after this handler has returned.
            new_process_group=True,
        )

        try:
            result = await self._run_with_process(
                payload, run_context, process, command, settings
            )
        except BaseException:
            # Anything escaping the drive that the paths inside do not already
            # handle -- a stream that failed its size limit or its encoding
            # (`stdout_lines` raises `RuntimeError`), a bug in a frame handler.
            # Without this the process is left running with write access to the
            # project and holding its subprocess slot. `_abort` is idempotent,
            # so running it again after one of those paths costs nothing.
            await self._abort(process)
            raise

        # Stamped on the way out of every path rather than at each construction
        # site inside `_drive`: all of them sit within the window being
        # measured, and elapsed time is as much a part of a failed run's account
        # as of a settled one's.
        result.duration_sec = time.monotonic() - started
        return result

    async def _run_with_process(
        self,
        payload: RunAgentTaskRunPayload,
        run_context: RunAgentTaskRunContext,
        process: icommandrunner.IAsyncProcess,
        command: str,
        settings: _ClaudeRunSettings,
    ) -> RunAgentTaskRunResult:
        async with run_context.progress("Agent task", cancellable=True) as progress:
            try:
                result = await asyncio.wait_for(
                    self._drive(process, payload.prompt, progress),
                    timeout=settings.settle_timeout_sec,
                )
            except asyncio.CancelledError:
                # The caller withdrew. Nothing is returned on this path, so
                # whatever the run spent before being cancelled is lost with it
                # -- the same reason the `ABORTED` status is never produced.
                await self._abort(process)
                raise
            except TimeoutError:
                await self._abort(process)
                return RunAgentTaskRunResult(
                    status=AgentRunStatus.FAILED,
                    error=(
                        f"claude did not finish within {settings.settle_timeout_sec}s"
                    ),
                )

        return self._check_structured_output(
            payload, await self._finish(process, result, command)
        )

    def _check_structured_output(
        self,
        payload: RunAgentTaskRunPayload,
        result: RunAgentTaskRunResult,
    ) -> RunAgentTaskRunResult:
        """A settled run that was asked for structured output must have produced it.

        The CLI can settle without calling the structured-output tool -- for
        instance when the answer did not need it -- and reporting that as a
        success would hand the caller `None` where it asked for a report.
        """
        if payload.output_schema is None or result.status is not AgentRunStatus.SETTLED:
            return result
        if result.structured_output is None:
            return dataclasses.replace(
                result,
                status=AgentRunStatus.FAILED,
                error="claude settled without structured output",
            )
        return result

    async def _drive(
        self,
        process: icommandrunner.IAsyncProcess,
        prompt: str,
        progress: code_action.ProgressContext,
    ) -> RunAgentTaskRunResult:
        # Subscribe before prompting. The stream replays anything produced
        # earlier, so this is not load-bearing -- it just keeps the order on the
        # page the same as the order on the wire.
        lines = process.stdout_lines()
        process.write_to_stdin(prompt)
        process.close_stdin()

        streamed: list[str] = []
        """Assistant text as it arrives.

        The fallback answer only: the `result` frame carries the same text
        already assembled, and is what a settled run reports. This exists for
        the runs that never get one.
        """
        turns = 0
        model: str | None = None
        outcome: claude_code_stream.RunResult | None = None

        async for line in lines:
            frame = claude_code_stream.parse_frame(line)
            if frame is None:
                self.logger.debug(f"Ignoring non-JSON line from claude: {line[:200]}")
                continue

            event = frame.get("type")

            if event == claude_code_stream.ClaudeEvent.SYSTEM:
                if frame.get("subtype") == "init":
                    reported_model = frame.get("model")
                    model = reported_model if isinstance(reported_model, str) else None
                    session = claude_code_stream.session_id(frame)
                    # Logged rather than returned: the transcript it points at
                    # is how a human inspects afterwards what the agent did to
                    # their files, and a failed run is exactly when that is
                    # wanted.
                    self.logger.info(f"claude session {session} on model {model}")
                continue

            if event == claude_code_stream.ClaudeEvent.ASSISTANT:
                turns += 1
                streamed.append(claude_code_stream.assistant_text(frame))
                # Tool names, never the model's own text (R-304): that text is
                # result data and belongs in `output`.
                tools = claude_code_stream.assistant_tool_names(frame)
                for tool in tools:
                    await progress.report(f"Tool: {tool}")
                if not tools:
                    await progress.report(f"Turn {turns}")
                continue

            if event == claude_code_stream.ClaudeEvent.RESULT:
                outcome = claude_code_stream.parse_result(frame, model)
                break

        if outcome is None:
            # The stream reached EOF without a `result` frame: claude died, or
            # was killed, mid-run. Whatever text arrived first is kept -- it is
            # still the best account of what happened -- but reporting SETTLED
            # here would tell the caller a truncated run finished its work.
            return RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED,
                output="".join(streamed),
                turns=turns or None,
                error="claude stopped without reporting a result",
            )

        return RunAgentTaskRunResult(
            status=self._status(outcome),
            output=outcome.output or "".join(streamed),
            turns=outcome.turns if outcome.turns is not None else turns or None,
            usage=outcome.usage,
            error=self._error(outcome),
            structured_output=outcome.structured_output,
        )

    def _status(self, outcome: claude_code_stream.RunResult) -> AgentRunStatus:
        """The outcome, distinguishing a refusal to guess from a failure.

        A denial recorded on a run that still succeeded says nothing: the agent
        asked for something, was told no, and found another way. A denial on a
        run that did *not* succeed is the reportable case -- the run needed a
        decision this setup was configured not to make, which is
        `REFUSED_INTERACTION` rather than `FAILED`, and is the distinction a
        non-interactive caller acts on.
        """
        if outcome.settled:
            return AgentRunStatus.SETTLED
        if outcome.denied_tools:
            return AgentRunStatus.REFUSED_INTERACTION
        return AgentRunStatus.FAILED

    def _error(self, outcome: claude_code_stream.RunResult) -> str | None:
        if outcome.settled:
            return None
        if not outcome.denied_tools:
            return outcome.error

        denied = ", ".join(sorted(set(outcome.denied_tools)))
        return (
            f"claude was denied permission to use {denied} and no interactive "
            f"channel is available ({outcome.error})"
        )

    async def _finish(
        self,
        process: icommandrunner.IAsyncProcess,
        result: RunAgentTaskRunResult,
        command: str,
    ) -> RunAgentTaskRunResult:
        with contextlib.suppress(TimeoutError):
            await process.wait_for_end(timeout=_EXIT_GRACE_SEC)

        exit_code = process.get_exit_code()
        stderr = process.get_error_output()
        self.logger.debug(f"claude exited with {exit_code}; stderr: {stderr}")

        if exit_code not in (0, None) and result.status is not (
            AgentRunStatus.REFUSED_INTERACTION
        ):
            # A refusal already explains itself, and a refused run is expected
            # to exit non-zero -- reporting that instead would replace the real
            # reason with a symptom.
            reason = f"claude exited with code {exit_code}: {stderr.strip()}"
            return dataclasses.replace(
                result,
                status=AgentRunStatus.FAILED,
                error=f"{result.error}; {reason}" if result.error else reason,
            )

        if result.status is AgentRunStatus.SETTLED and not result.output:
            # R-503: the handler's job is to produce output, so producing none
            # is diagnosable rather than merely uninteresting.
            self.user_messenger.warning(
                f"Agent settled without output (exit {exit_code}). Command: {command}"
            )

        return result

    async def _abort(self, process: icommandrunner.IAsyncProcess) -> None:
        """Stop claude, escalating until it is actually gone.

        There is no abort message to send: stdin carried the prompt and was
        closed with it, and print mode has no control channel. So the grace
        period is all the politeness available, and after it the ladder has to
        become unconditional -- a claude that outlives its own timeout keeps
        write access to the project a caller may already be restoring, keeps
        spending budget, and keeps holding the subprocess slot the timeout
        exists to release.

        Best-effort throughout: this runs where something has already gone wrong
        or been withdrawn, and a teardown failure must not replace the original
        outcome. Safe to call twice.
        """
        with contextlib.suppress(Exception):
            process.close_stdin()
        with contextlib.suppress(Exception):
            await process.wait_for_end(timeout=_EXIT_GRACE_SEC)

        if not process.is_alive():
            return

        self.logger.warning(
            f"claude did not exit within {_EXIT_GRACE_SEC}s; terminating it"
        )
        with contextlib.suppress(Exception):
            process.terminate()
        with contextlib.suppress(Exception):
            await process.wait_for_end(timeout=_SIGNAL_GRACE_SEC)

        if not process.is_alive():
            return

        self.logger.warning("claude ignored SIGTERM; killing it")
        with contextlib.suppress(Exception):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait_for_end(timeout=_SIGNAL_GRACE_SEC)

    def _resolve(self, profile: str | None) -> _ClaudeRunSettings | str:
        """The run settings for *profile*, or an error message to fail with.

        `None` is the top-level settings, so an existing caller that sends no
        profile gets exactly today's command.
        """
        if profile is None:
            return _ClaudeRunSettings(
                model=self.config.model,
                settle_timeout_sec=self.config.settle_timeout_sec,
            )
        resolved = self.config.profiles.get(profile)
        if resolved is None:
            return backend_support.unknown_profile_error(profile, self.config.profiles)
        return _ClaudeRunSettings(
            model=resolved.model if resolved.model is not None else self.config.model,
            settle_timeout_sec=(
                resolved.settle_timeout_sec
                if resolved.settle_timeout_sec is not None
                else self.config.settle_timeout_sec
            ),
        )

    def _executable(self) -> list[str]:
        """The program and fixed arguments, before the mode and run flags.

        A seam for tests: the fake swaps this rather than `_build_command`, so
        the flags under test are still assembled by the production code.
        """
        return ["claude"]

    def _build_command(
        self,
        settings: _ClaudeRunSettings,
        output_schema: dict[str, object] | None = None,
    ) -> str:
        # `--verbose` is not optional: the CLI rejects `stream-json` output in
        # print mode without it.
        parts = [
            *self._executable(),
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
        ]

        if settings.model is not None:
            parts += ["--model", settings.model]
        if self.config.permission_mode is not None:
            parts += ["--permission-mode", self.config.permission_mode]
        if self.config.allowed_tools:
            parts += ["--allowed-tools", *self.config.allowed_tools]
        if self.config.disallowed_tools:
            parts += ["--disallowed-tools", *self.config.disallowed_tools]
        if self.config.append_system_prompt is not None:
            parts += ["--append-system-prompt", self.config.append_system_prompt]
        if self.config.max_budget_usd is not None:
            parts += ["--max-budget-usd", str(self.config.max_budget_usd)]
        if output_schema is not None:
            # A schema is a few KB, so it travels as an argv string rather than
            # through a temp file that would need cleaning up on every path.
            parts += ["--json-schema", json.dumps(output_schema)]

        return shlex.join(parts)
