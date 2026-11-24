import asyncio
import contextlib
import dataclasses
import json
import pathlib
import time
import typing
import uuid

from fine_agent import backend_support, question_support
from fine_agent.history_types import (
    AgentBackend,
    AgentRunObservation,
    HistoryDiscoveryStatus,
    NativeHistoryRole,
    RelatedHistoryStatus,
    UsageProvenance,
    UsageScope,
    UsageSource,
)
from fine_agent.question_support import QuestionShape
from fine_agent.run_agent_task_action import (
    AgentRunStatus,
    AgentSessionRef,
    RunAgentTaskAction,
    RunAgentTaskRunContext,
    RunAgentTaskRunPayload,
    RunAgentTaskRunResult,
)
from fine_agent.session_support import (
    session_backend_error,
    session_id_error,
    session_project_error,
    with_session,
)
from fine_agent.snapshot_stream import SnapshotStream
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    icommandrunner,
    ilogger,
    iprojectactionrunner,
    iprojectinfoprovider,
    iuser_messenger,
    iuserprompt,
)

from fine_agent_claude_code import (
    claude_code_control,
    claude_code_history,
    claude_code_questions,
    claude_code_stream,
)
from fine_agent_claude_code.claude_code_history import ClaudeObservationState

_EXIT_GRACE_SEC = 5.0
"""How long to let claude exit once its stream has ended.

Short on purpose: by this point the run has already produced its answer or been
withdrawn, and the only thing still being waited for is the process noticing
that its input is closed.
"""

_VERSION_TIMEOUT_SEC = 3.0
"""Bound for the post-run `--version` probe on captured runs."""

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
    thinking: str | None = None
    settle_timeout_sec: float | None = None
    may_ask_questions: bool | None = None


@dataclasses.dataclass
class ClaudeCodeAgentHandlerConfig(code_action.ActionHandlerConfig):
    model: str | None = None
    """Model passed to `claude --model`, as an alias (`opus`, `sonnet`) or a
    full name. `None` leaves the CLI's own default.

    Lives here rather than on the payload so the same task definition runs
    against a different model without being edited (PRD-0005 R3).
    """
    thinking: str | None = None
    """Thinking level passed to `claude --effort` (`low`, `medium`, `high`,
    `xhigh`, `max`). `None` leaves the CLI's own default, which depends on the
    model and the CLI version -- set it where a run's depth matters."""
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
    this a wedged run never ends."""
    may_ask_questions: bool = False
    """Opt in to routing claude's permission prompts to the run's person."""
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


class _QuestionUnavailable(Exception):
    """An ask ended with nobody to ask; the drive aborts the run."""

    def __init__(self, question: str) -> None:
        super().__init__(question)
        self.question = question


def _request_question(request: dict[str, typing.Any]) -> str:
    """The question text a budget error names for a pending request."""
    tool_name = request.get("tool_name")
    tool_input = request.get("input")
    if tool_name == "AskUserQuestion" and isinstance(tool_input, dict):
        entries = claude_code_questions.parse_ask_user_question(tool_input)
        if entries:
            return entries[0].question
        return "an unrepresentable question"
    if isinstance(tool_name, str):
        return claude_code_questions.permission_question(tool_name)
    return "a pending permission request"


def _clock_blocked(clock: _QuestionClock) -> bool:
    """Whether the settle budget is paused while requests are pending."""
    ids = {
        entry.tool_use_id
        for entry in clock.pending.values()
        if entry.tool_use_id is not None
    }
    if not ids:
        return False
    unanswered = clock.tool_use_seen - clock.tool_result_seen
    return all(tool_use_id in ids for tool_use_id in unanswered)


@dataclasses.dataclass
class _PendingRequest:
    """A `can_use_tool` request the host has not answered yet."""

    request_id: str
    tool_use_id: str | None
    question: str
    task: asyncio.Task[None] | None = None
    withdrawn: bool = False


@dataclasses.dataclass
class _QuestionClock:
    """Shared blocked-signal state between the question drive and the clock."""

    pending: dict[str, _PendingRequest] = dataclasses.field(default_factory=dict)
    tool_use_seen: set[str] = dataclasses.field(default_factory=set)
    tool_result_seen: set[str] = dataclasses.field(default_factory=set)
    init_session_id: str | None = None
    changed: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)


@dataclasses.dataclass(frozen=True)
class _ClaudeRunSettings:
    """The claude run knobs for one run, after a profile is resolved."""

    model: str | None
    thinking: str | None
    settle_timeout_sec: float
    may_ask_questions: bool = False


def _unknown_provenance() -> UsageProvenance:
    return UsageProvenance(
        token_source=UsageSource.UNKNOWN,
        token_scope=UsageScope.UNKNOWN,
        cost_source=UsageSource.UNKNOWN,
        cost_scope=UsageScope.UNKNOWN,
    )


def _pre_session_observation(gaps: list[str]) -> AgentRunObservation:
    return AgentRunObservation(
        backend=AgentBackend.CLAUDE_CODE,
        backend_version=None,
        session_id=None,
        history_root=None,
        history_status=HistoryDiscoveryStatus.UNAVAILABLE,
        files=[],
        related_status=RelatedHistoryStatus.UNAVAILABLE,
        usage_provenance=_unknown_provenance(),
        gaps=list(gaps),
    )


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
    quoting problem. Nothing is sent afterwards in a run without questions --
    stdin closes immediately, which is also why there is no in-band way to
    abort a run once it starts (see `_abort`). A question-mode run keeps stdin
    open for permission replies until the `result` frame, then closes it.

    Exit codes: non-zero is treated as failure with stderr attached, but `0` is
    *not* sufficient for success. The outcome is decided from the `result`
    frame; the exit code only adds to it.

    One complete snapshot is sent per assistant frame, with no time-driven
    flush (i = infinity). For M assistant messages over T seconds, at most
    M + ceil(T / i) + 1, or M + 1, partials are sent. Snapshot text across
    those hops is bounded by that count times the longest snapshot; terminal
    fields are not part of this bound.
    """

    def __init__(
        self,
        config: ClaudeCodeAgentHandlerConfig,
        logger: ilogger.ILogger,
        command_runner: icommandrunner.ICommandRunner,
        user_messenger: iuser_messenger.IUserMessenger,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        user_prompt: iuserprompt.IUserPrompt,
    ) -> None:
        self.config = config
        self.logger = logger
        self.command_runner = command_runner
        self.user_messenger = user_messenger
        self.project_info_provider = project_info_provider
        self.user_prompt = user_prompt

    async def run(
        self,
        payload: RunAgentTaskRunPayload,
        run_context: RunAgentTaskRunContext,
    ) -> RunAgentTaskRunResult:
        snapshots = SnapshotStream(
            run_context.partial_result_sender,
            min_interval_sec=None,
            logger=self.logger,
        )
        result = await self._run_with_snapshots(payload, run_context, snapshots)
        return await snapshots.finish(result)

    async def _run_with_snapshots(
        self,
        payload: RunAgentTaskRunPayload,
        run_context: RunAgentTaskRunContext,
        snapshots: SnapshotStream,
    ) -> RunAgentTaskRunResult:
        if payload.continue_session is not None:
            return await self._run_continuation(payload, run_context, snapshots)
        settings = self._resolve(payload.profile)
        if isinstance(settings, str):
            # Fail before spawning anything: an unknown profile is a
            # configuration error, not a run that got partway.
            result = RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED, error=settings, duration_sec=0.0
            )
            if payload.capture_history:
                observation = _pre_session_observation(
                    ["no native session was available"]
                )
                version, version_gap = await self._probe_backend_version()
                gaps = [*observation.gaps]
                if version_gap is not None:
                    gaps.append(version_gap)
                observation = dataclasses.replace(
                    observation, backend_version=version, gaps=gaps
                )
                result = dataclasses.replace(result, observation=observation)
            return result
        start = await self._question_start(payload, settings)
        if start.mode is question_support.AgentQuestionMode.REFUSED:
            return self._refused_at_question_start(payload, start.refusal_error)
        command = self._build_command(
            settings,
            payload.output_schema,
            questions=start.mode is question_support.AgentQuestionMode.WITH_QUESTIONS,
        )
        project_dir = self.project_info_provider.get_current_project_dir_path()
        self.logger.debug(f"Starting claude: {command!r} in {project_dir}")

        started = time.monotonic()
        try:
            process = await self.command_runner.start_long_running(
                command,
                cwd=project_dir,
                # claude runs tools of its own, so tearing it down has to reach them
                # too -- otherwise an abandoned run leaves a subtree editing the
                # project after this handler has returned.
                new_process_group=True,
            )
        except (OSError, icommandrunner.CommandNotLaunchableError) as error:
            # The program could not be started at all -- missing from PATH, or
            # (on Windows) a shim refusing an argument cmd.exe would
            # reinterpret. A settled-with-error result is still a structured
            # one a caller can read.
            result = RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED,
                error=backend_support.spawn_error(command[0], error),
                duration_sec=0.0,
            )
            if payload.capture_history:
                observation = _pre_session_observation(
                    ["no native session was available"]
                )
                version, version_gap = await self._probe_backend_version()
                gaps = [*observation.gaps]
                if version_gap is not None:
                    gaps.append(version_gap)
                observation = dataclasses.replace(
                    observation, backend_version=version, gaps=gaps
                )
                result = dataclasses.replace(result, observation=observation)
            return result

        return await self._settle_spawned_run(
            payload,
            run_context,
            process,
            command,
            settings,
            project_dir,
            started,
            snapshots,
            questions=start.mode is question_support.AgentQuestionMode.WITH_QUESTIONS,
        )

    async def _settle_spawned_run(
        self,
        payload: RunAgentTaskRunPayload,
        run_context: RunAgentTaskRunContext,
        process: icommandrunner.IAsyncProcess,
        command: list[str],
        settings: _ClaudeRunSettings,
        project_dir: pathlib.Path,
        started: float,
        snapshots: SnapshotStream,
        questions: bool = False,
    ) -> RunAgentTaskRunResult:
        try:
            result, observation_state = await self._run_with_process(
                payload,
                run_context,
                process,
                command,
                settings,
                snapshots,
                project_dir,
                questions,
            )
        except BaseException:
            # Anything escaping the drive that the paths inside do not already
            # handle -- a stream that failed its size limit or its encoding
            # (`stdout_lines` raises `RuntimeError`), a bug in a frame handler.
            # Without this the process is left running with write access to the
            # project. `_abort` is idempotent,
            # so running it again after one of those paths costs nothing.
            await self._abort(process)
            raise

        # Stamped on the way out of every path rather than at each construction
        # site inside `_drive`: all of them sit within the window being
        # measured, and elapsed time is as much a part of a failed run's account
        # as of a settled one's.
        result.duration_sec = time.monotonic() - started
        result = with_session(
            result,
            backend=AgentBackend.CLAUDE_CODE,
            project_dir=project_dir,
            reported_id=observation_state.session_id,
            expected=payload.continue_session,
        )
        if payload.capture_history:
            try:
                result = await self._attach_observation(result, observation_state)
            except Exception as error:  # noqa: BLE001
                gaps = [*observation_state.gaps, f"observation failed: {error}"]
                self.logger.debug(f"claude observation failed: {error}")
                failed = AgentRunObservation(
                    backend=AgentBackend.CLAUDE_CODE,
                    backend_version=None,
                    session_id=observation_state.session_id,
                    history_root=None,
                    history_status=HistoryDiscoveryStatus.FAILED,
                    files=[],
                    related_status=RelatedHistoryStatus.UNAVAILABLE,
                    usage_provenance=observation_state.usage_provenance,
                    gaps=gaps,
                )
                result = dataclasses.replace(result, observation=failed)
        return result

    def _continuation_failure(
        self,
        payload: RunAgentTaskRunPayload,
        error: str,
        started: float,
        observation: AgentRunObservation | None = None,
    ) -> RunAgentTaskRunResult:
        """A continuation failure before any native process was started.

        Never runs a version probe; with capture, the run carries an
        unavailable observation naming why no session was resumed.
        """
        failed = RunAgentTaskRunResult(
            status=AgentRunStatus.FAILED,
            error=error,
            duration_sec=time.monotonic() - started,
        )
        if payload.capture_history:
            if observation is None:
                observation = _pre_session_observation([error])
            failed = dataclasses.replace(failed, observation=observation)
        return failed

    async def _run_continuation(
        self,
        payload: RunAgentTaskRunPayload,
        run_context: RunAgentTaskRunContext,
        snapshots: SnapshotStream,
    ) -> RunAgentTaskRunResult:
        ref = payload.continue_session
        assert ref is not None
        started = time.monotonic()
        failure = session_id_error(ref.session_id)
        if failure is None:
            failure = session_backend_error(ref, AgentBackend.CLAUDE_CODE)
        project_dir: pathlib.Path | None = None
        if failure is None:
            project_dir = self.project_info_provider.get_current_project_dir_path()
            failure = session_project_error(ref, project_dir)
        if failure is not None:
            return self._continuation_failure(payload, failure, started)
        if payload.profile is not None and (
            payload.profile not in self.config.profiles
        ):
            unknown = backend_support.unknown_profile_error(
                payload.profile, self.config.profiles
            )
            return self._continuation_failure(
                payload,
                unknown,
                started,
                observation=_pre_session_observation([unknown]),
            )
        assert project_dir is not None

        lookup_state = ClaudeObservationState(
            session_id=ref.session_id,
            usage_provenance=_unknown_provenance(),
            gaps=[],
        )
        observation = await asyncio.to_thread(
            claude_code_history.locate_claude_history,
            lookup_state,
            claude_code_history.claude_config_root(),
        )
        main = next(
            (f for f in observation.files if f.role is NativeHistoryRole.MAIN),
            None,
        )
        if main is None:
            reason = "; ".join(observation.gaps) or (
                f"claude session {ref.session_id!r} was not found"
            )
            return self._continuation_failure(
                payload, reason, started, observation=observation
            )

        settings = self._resolve(payload.profile)
        if isinstance(settings, str):
            # Unreachable through the membership check above: `_resolve`
            # only reports unknown profiles. Kept graceful rather than
            # asserted so a future resolver error still fails the run.
            return self._continuation_failure(payload, settings, started)
        if settings.model is None:
            profile_name = (
                f"profile {payload.profile!r}"
                if payload.profile is not None
                else "the top-level settings"
            )
            return self._continuation_failure(
                payload,
                f"cannot continue claude session {ref.session_id!r} without an "
                f"explicit model: the handler cannot reset the saved session "
                f"model to the native new-run default; set 'model' in "
                f"{profile_name}",
                started,
            )
        if self.config.permission_mode is None:
            return self._continuation_failure(
                payload,
                f"cannot continue claude session {ref.session_id!r} without an "
                f"explicit permission mode: the handler cannot reset the "
                f"saved session permissions to the native new-run default; "
                f"set 'permission_mode' in the handler config",
                started,
            )
        start = await self._question_start(payload, settings)
        if start.mode is question_support.AgentQuestionMode.REFUSED:
            return self._refused_at_question_start(payload, start.refusal_error)

        command = self._build_command(
            settings,
            payload.output_schema,
            continue_session=ref,
            questions=start.mode is question_support.AgentQuestionMode.WITH_QUESTIONS,
        )
        self.logger.debug(f"Continuing claude: {command!r} in {project_dir}")

        try:
            process = await self.command_runner.start_long_running(
                command,
                cwd=project_dir,
                new_process_group=True,
            )
        except (OSError, icommandrunner.CommandNotLaunchableError) as error:
            failed = RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED,
                error=backend_support.spawn_error(command[0], error),
                duration_sec=time.monotonic() - started,
            )
            if payload.capture_history:
                failed = dataclasses.replace(
                    failed, observation=_pre_session_observation([failed.error or ""])
                )
            return failed

        return await self._settle_spawned_run(
            payload,
            run_context,
            process,
            command,
            settings,
            project_dir,
            started,
            snapshots,
            questions=start.mode is question_support.AgentQuestionMode.WITH_QUESTIONS,
        )

    async def _run_with_process(
        self,
        payload: RunAgentTaskRunPayload,
        run_context: RunAgentTaskRunContext,
        process: icommandrunner.IAsyncProcess,
        command: list[str],
        settings: _ClaudeRunSettings,
        snapshots: SnapshotStream,
        project_dir: pathlib.Path,
        questions: bool = False,
    ) -> tuple[RunAgentTaskRunResult, ClaudeObservationState]:
        async with run_context.progress("Agent task", cancellable=True) as progress:
            try:
                if questions:
                    clock = _QuestionClock()
                    drive = self._drive_questions(
                        process, payload.prompt, progress, snapshots, clock
                    )
                    result, observation_state = await self._run_with_movable_budget(
                        drive, settings, clock, process
                    )
                else:
                    drive = self._drive(process, payload.prompt, progress, snapshots)
                    result, observation_state = await asyncio.wait_for(
                        drive,
                        timeout=settings.settle_timeout_sec,
                    )
            except asyncio.CancelledError:
                # The caller withdrew. Nothing is returned on this path, so
                # whatever the run spent before being cancelled is lost with it
                # -- the same reason the `ABORTED` status is never produced.
                await self._abort(process)
                raise
            except iprojectactionrunner.ActionRunCancelled:
                await self._abort(process)
                raise
            except TimeoutError:
                await self._abort(process)
                timed_out = RunAgentTaskRunResult(
                    status=AgentRunStatus.FAILED,
                    error=(
                        f"claude did not finish within {settings.settle_timeout_sec}s"
                    ),
                )
                timed_out_state = ClaudeObservationState(
                    session_id=None,
                    usage_provenance=_unknown_provenance(),
                    gaps=["claude did not finish; session was not collected"],
                )
                return timed_out, timed_out_state

        if (
            questions
            and result.status is not AgentRunStatus.SETTLED
            and observation_state.session_id is not None
        ):
            result = with_session(
                result,
                backend=AgentBackend.CLAUDE_CODE,
                project_dir=project_dir,
                reported_id=observation_state.session_id,
                expected=payload.continue_session,
                report_unsettled_session=True,
            )
        finished = await self._finish(process, result, command)
        return self._check_structured_output(payload, finished), observation_state

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

    async def _run_with_movable_budget(
        self,
        drive: typing.Coroutine[
            typing.Any, typing.Any, tuple[RunAgentTaskRunResult, ClaudeObservationState]
        ],
        settings: _ClaudeRunSettings,
        clock: _QuestionClock,
        process: icommandrunner.IAsyncProcess,
    ) -> tuple[RunAgentTaskRunResult, ClaudeObservationState]:
        """Run the question drive with a budget that pauses while blocked."""
        drive_task = asyncio.get_running_loop().create_task(drive)
        deadline = time.monotonic() + settings.settle_timeout_sec
        try:
            while not drive_task.done():
                blocked = _clock_blocked(clock)
                if blocked:
                    remaining = None
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                clock.changed.clear()
                waiter = asyncio.get_running_loop().create_task(clock.changed.wait())
                blocked_since = time.monotonic()
                try:
                    done, _ = await asyncio.wait(
                        {drive_task, waiter},
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    if not waiter.done():
                        _ = waiter.cancel()
                        await asyncio.gather(waiter, return_exceptions=True)
                if drive_task in done:
                    break
                if blocked:
                    deadline += time.monotonic() - blocked_since
                    continue
                if not done:
                    break
            if drive_task.done():
                if drive_task.cancelled():
                    raise asyncio.CancelledError
                exc = drive_task.exception()
                if exc is not None:
                    raise exc
                return drive_task.result()
            snapshot = [
                entry.question for entry in clock.pending.values() if entry.question
            ]
            _ = drive_task.cancel()
            await asyncio.gather(drive_task, return_exceptions=True)
            await self._abort(process)
            if snapshot:
                question = snapshot[0]
                return RunAgentTaskRunResult(
                    status=AgentRunStatus.REFUSED_INTERACTION,
                    output="",
                    turns=None,
                    error=question_support.settle_budget_question_error(
                        question, settings.settle_timeout_sec
                    ),
                ), ClaudeObservationState(
                    session_id=clock.init_session_id,
                    usage_provenance=_unknown_provenance(),
                    gaps=[],
                )
            return RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED,
                error=(f"claude did not finish within {settings.settle_timeout_sec}s"),
            ), ClaudeObservationState(
                session_id=None,
                usage_provenance=_unknown_provenance(),
                gaps=["claude did not finish; session was not collected"],
            )
        finally:
            if not drive_task.done():
                _ = drive_task.cancel()
                await asyncio.gather(drive_task, return_exceptions=True)

    async def _drive(
        self,
        process: icommandrunner.IAsyncProcess,
        prompt: str,
        progress: code_action.ProgressContext,
        snapshots: SnapshotStream,
    ) -> tuple[RunAgentTaskRunResult, ClaudeObservationState]:
        # Subscribe before prompting. The stream replays anything produced
        # earlier, so this is not load-bearing -- it just keeps the order on the
        # page the same as the order on the wire.
        lines = process.stdout_lines()
        streamed: list[str] = []
        """Assistant text as it arrives.

        The fallback answer only: the `result` frame carries the same text
        already assembled, and is what a settled run reports. This exists for
        the runs that never get one.
        """
        turns = 0
        try:
            process.write_to_stdin(prompt)
            process.close_stdin()
        except (BrokenPipeError, ConnectionResetError) as error:
            state = ClaudeObservationState(
                session_id=None,
                usage_provenance=_unknown_provenance(),
                gaps=[f"claude closed its prompt pipe before reading it: {error}"],
            )
            return RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED,
                output="".join(streamed),
                turns=turns or None,
                error=f"claude closed its prompt pipe before reading it: {error}",
            ), state
        model: str | None = None
        outcome: claude_code_stream.RunResult | None = None
        init_session_id: str | None = None
        result_session_id: str | None = None
        result_provenance: UsageProvenance = _unknown_provenance()

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
                    init_session_id = session
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
                await snapshots.update("".join(streamed), message_end=True)
                continue

            if event == claude_code_stream.ClaudeEvent.RESULT:
                result_session_id = claude_code_stream.session_id(frame)
                result_provenance = claude_code_stream.result_provenance(frame)
                outcome = claude_code_stream.parse_result(frame, model)
                break

        def _observation_state(
            provenance: UsageProvenance,
        ) -> ClaudeObservationState:
            gaps: list[str] = []
            session: str | None = None
            if init_session_id is not None and result_session_id is not None:
                if init_session_id != result_session_id:
                    gaps.append(
                        "claude session id disagreement between init and result"
                    )
                else:
                    session = init_session_id
            elif init_session_id is not None:
                session = init_session_id
            elif result_session_id is not None:
                session = result_session_id
            else:
                gaps.append("claude reported no native session id")
            return ClaudeObservationState(
                session_id=session,
                usage_provenance=provenance,
                gaps=gaps,
            )

        if outcome is None:
            # The stream reached EOF without a `result` frame: claude died, or
            # was killed, mid-run. Whatever text arrived first is kept -- it is
            # still the best account of what happened -- but reporting SETTLED
            # here would tell the caller a truncated run finished its work.
            state = _observation_state(_unknown_provenance())
            return RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED,
                output="".join(streamed),
                turns=turns or None,
                error="claude stopped without reporting a result",
            ), state

        state = _observation_state(result_provenance)
        return RunAgentTaskRunResult(
            status=self._status(outcome),
            output=outcome.output or "".join(streamed),
            turns=outcome.turns if outcome.turns is not None else turns or None,
            usage=outcome.usage,
            error=self._error(outcome),
            structured_output=outcome.structured_output,
        ), state

    async def _drive_questions(
        self,
        process: icommandrunner.IAsyncProcess,
        prompt: str,
        progress: code_action.ProgressContext,
        snapshots: SnapshotStream,
        clock: _QuestionClock,
    ) -> tuple[RunAgentTaskRunResult, ClaudeObservationState]:
        """Drive a run that routes permission prompts to the run's person."""
        lines = process.stdout_lines()
        streamed: list[str] = []
        turns = 0
        queue: asyncio.Queue[tuple[str, typing.Any]] = asyncio.Queue()
        pending = clock.pending
        tool_use_seen = clock.tool_use_seen
        tool_result_seen = clock.tool_result_seen
        decided_ids: set[str] = set()
        ask_lock = asyncio.Lock()
        stop_event = asyncio.Event()
        tasks: set[asyncio.Task[None]] = set()

        async def _pump() -> None:
            try:
                async for line in lines:
                    queue.put_nowait(("line", line))
                queue.put_nowait(("eof", None))
            except BaseException as exc:
                queue.put_nowait(("error", exc))

        def _spawn(frame: dict[str, typing.Any]) -> None:
            raw_request = frame.get("request")
            request_map = raw_request if isinstance(raw_request, dict) else {}
            raw_tool_use_id = request_map.get("tool_use_id")
            entry = _PendingRequest(
                request_id=str(frame.get("request_id")),
                tool_use_id=(
                    raw_tool_use_id if isinstance(raw_tool_use_id, str) else None
                ),
                question=_request_question(request_map),
            )
            pending[entry.request_id] = entry
            clock.changed.set()
            task = asyncio.get_running_loop().create_task(
                self._route_request(
                    process,
                    frame,
                    entry,
                    pending,
                    decided_ids,
                    ask_lock,
                    stop_event,
                    queue,
                    clock,
                )
            )
            entry.task = task
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        init_id = f"req_1_{uuid.uuid4().hex[:8]}"
        try:
            process.write_to_stdin(claude_code_control.initialize_line(init_id))
        except (BrokenPipeError, ConnectionResetError) as error:
            state = ClaudeObservationState(
                session_id=None,
                usage_provenance=_unknown_provenance(),
                gaps=[f"claude closed its prompt pipe before reading it: {error}"],
            )
            return RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED,
                output="".join(streamed),
                turns=turns or None,
                error=f"claude closed its prompt pipe before reading it: {error}",
            ), state
        model: str | None = None
        outcome: claude_code_stream.RunResult | None = None
        init_session_id: str | None = None
        result_session_id: str | None = None
        result_provenance: UsageProvenance = _unknown_provenance()
        initialized = False
        init_error: str | None = None
        unavailable_question: str | None = None
        pump = asyncio.get_running_loop().create_task(_pump())
        try:
            while True:
                kind, item = await queue.get()
                if kind == "error":
                    raise item
                if kind == "unavailable":
                    unavailable_question = typing.cast(str, item)
                    break
                if kind == "abandon":
                    raise typing.cast(BaseException, item)
                if kind == "eof":
                    break
                frame = claude_code_stream.parse_frame(typing.cast(str, item))
                if frame is None:
                    self.logger.debug(
                        f"Ignoring non-JSON line from claude: {str(item)[:200]}"
                    )
                    continue
                event = frame.get("type")
                if event == claude_code_stream.ClaudeEvent.SYSTEM:
                    if frame.get("subtype") == "init":
                        reported_model = frame.get("model")
                        model = (
                            reported_model if isinstance(reported_model, str) else None
                        )
                        session = claude_code_stream.session_id(frame)
                        init_session_id = session
                        clock.init_session_id = session
                        self.logger.info(f"claude session {session} on model {model}")
                    continue
                if event == claude_code_stream.ClaudeEvent.ASSISTANT:
                    turns += 1
                    streamed.append(claude_code_stream.assistant_text(frame))
                    for tool_use_id in claude_code_stream.tool_use_ids(frame):
                        if tool_use_id not in tool_use_seen:
                            tool_use_seen.add(tool_use_id)
                            clock.changed.set()
                    tools = claude_code_stream.assistant_tool_names(frame)
                    for tool in tools:
                        await progress.report(f"Tool: {tool}")
                    if not tools:
                        await progress.report(f"Turn {turns}")
                    await snapshots.update("".join(streamed), message_end=True)
                    continue
                if event == claude_code_stream.ClaudeEvent.USER:
                    for result_id in claude_code_stream.tool_result_ids(frame):
                        if result_id not in tool_result_seen:
                            tool_result_seen.add(result_id)
                            clock.changed.set()
                        self._withdraw_tool_use(pending, result_id, clock)
                    continue
                if event == claude_code_stream.ClaudeEvent.CONTROL_CANCEL_REQUEST:
                    request_id = frame.get("request_id")
                    if isinstance(request_id, str):
                        self._withdraw_request(pending, request_id, clock)
                    continue
                if event == claude_code_stream.ClaudeEvent.CONTROL_REQUEST:
                    request_id = frame.get("request_id")
                    if not isinstance(request_id, str):
                        self.logger.debug(
                            "Ignoring control request without a request id"
                        )
                        continue
                    request = frame.get("request")
                    subtype = (
                        request.get("subtype") if isinstance(request, dict) else None
                    )
                    if (
                        subtype
                        != claude_code_control.ControlRequestSubtype.CAN_USE_TOOL
                    ):
                        self._write_reply(
                            process,
                            claude_code_control.error_line(
                                request_id,
                                f"unknown control request subtype: {subtype}",
                            ),
                        )
                        continue
                    _spawn(frame)
                    # Let the router run to its first wait before reading on:
                    # a result frame already in the pipe must not overtake a
                    # request the router has not seen yet.
                    await asyncio.sleep(0)
                    continue
                if event == claude_code_stream.ClaudeEvent.CONTROL_RESPONSE:
                    response = frame.get("response")
                    if not isinstance(response, dict):
                        continue
                    if response.get("request_id") != init_id:
                        continue
                    if response.get("subtype") != "success":
                        raw_error = response.get("error")
                        init_error = (
                            raw_error
                            if isinstance(raw_error, str)
                            else str(response.get("subtype"))
                        )
                        break
                    initialized = True
                    try:
                        process.write_to_stdin(claude_code_control.user_line(prompt))
                    except (BrokenPipeError, ConnectionResetError) as error:
                        init_error = (
                            f"claude closed its prompt pipe before reading it: {error}"
                        )
                        break
                    continue
                if event == claude_code_stream.ClaudeEvent.RESULT:
                    if not initialized:
                        with contextlib.suppress(Exception):
                            process.close_stdin()
                    result_session_id = claude_code_stream.session_id(frame)
                    result_provenance = claude_code_stream.result_provenance(frame)
                    outcome = claude_code_stream.parse_result(frame, model)
                    break
        finally:
            _ = pump.cancel()
            for entry in list(pending.values()):
                entry.withdrawn = True
                if entry.task is not None:
                    _ = entry.task.cancel()
            for task in list(tasks):
                _ = task.cancel()
            unsettled_tasks = [task for task in list(tasks) if not task.done()]
            if not pump.done():
                unsettled_tasks.append(pump)
            if unsettled_tasks:
                await asyncio.gather(*unsettled_tasks, return_exceptions=True)
        if outcome is not None:
            with contextlib.suppress(Exception):
                process.close_stdin()

        def _observation_state(
            provenance: UsageProvenance,
        ) -> ClaudeObservationState:
            gaps: list[str] = []
            session: str | None = None
            if init_session_id is not None and result_session_id is not None:
                if init_session_id != result_session_id:
                    gaps.append(
                        "claude session id disagreement between init and result"
                    )
                else:
                    session = init_session_id
            elif init_session_id is not None:
                session = init_session_id
            elif result_session_id is not None:
                session = result_session_id
            else:
                gaps.append("claude reported no native session id")
            return ClaudeObservationState(
                session_id=session,
                usage_provenance=provenance,
                gaps=gaps,
            )

        if unavailable_question is not None:
            already_gone = not process.is_alive()
            await self._abort(process)
            if already_gone:
                state = _observation_state(_unknown_provenance())
                return RunAgentTaskRunResult(
                    status=AgentRunStatus.FAILED,
                    output="".join(streamed),
                    turns=turns or None,
                    error="claude stopped without reporting a result",
                ), state
            state = _observation_state(_unknown_provenance())
            return RunAgentTaskRunResult(
                status=AgentRunStatus.REFUSED_INTERACTION,
                output="".join(streamed),
                turns=turns or None,
                error=question_support.unanswered_question_error(unavailable_question),
            ), state
        if init_error is not None:
            with contextlib.suppress(Exception):
                process.close_stdin()
            state = _observation_state(_unknown_provenance())
            if init_error.startswith("claude closed its prompt pipe"):
                return RunAgentTaskRunResult(
                    status=AgentRunStatus.FAILED,
                    output="".join(streamed),
                    turns=turns or None,
                    error=init_error,
                ), state
            return RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED,
                output="".join(streamed),
                turns=turns or None,
                error=f"claude refused the control-protocol initialize: {init_error}",
            ), state
        if outcome is None:
            state = _observation_state(_unknown_provenance())
            return RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED,
                output="".join(streamed),
                turns=turns or None,
                error="claude stopped without reporting a result",
            ), state
        decided = frozenset(decided_ids)
        state = _observation_state(result_provenance)
        return RunAgentTaskRunResult(
            status=self._status(outcome, decided),
            output=outcome.output or "".join(streamed),
            turns=outcome.turns if outcome.turns is not None else turns or None,
            usage=outcome.usage,
            error=self._error(outcome, decided),
            structured_output=outcome.structured_output,
        ), state

    @staticmethod
    def _withdraw_request(
        pending: dict[str, _PendingRequest],
        request_id: str,
        clock: _QuestionClock | None = None,
    ) -> None:
        entry = pending.pop(request_id, None)
        if entry is None:
            return
        entry.withdrawn = True
        if clock is not None:
            clock.changed.set()
        if entry.task is not None:
            _ = entry.task.cancel()

    def _withdraw_tool_use(
        self,
        pending: dict[str, _PendingRequest],
        tool_use_id: str,
        clock: _QuestionClock | None = None,
    ) -> None:
        for request_id, entry in list(pending.items()):
            if entry.tool_use_id == tool_use_id:
                self._withdraw_request(pending, request_id, clock)

    @staticmethod
    def _write_reply(process: icommandrunner.IAsyncProcess, line: str) -> None:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            process.write_to_stdin(line)

    async def _route_request(
        self,
        process: icommandrunner.IAsyncProcess,
        frame: dict[str, typing.Any],
        entry: _PendingRequest,
        pending: dict[str, _PendingRequest],
        decided_ids: set[str],
        ask_lock: asyncio.Lock,
        stop_event: asyncio.Event,
        queue: asyncio.Queue[tuple[str, typing.Any]],
        clock: _QuestionClock | None = None,
    ) -> None:
        request_id = entry.request_id
        try:
            request = frame.get("request")
            tool_name = request.get("tool_name") if isinstance(request, dict) else None
            tool_input = request.get("input") if isinstance(request, dict) else None
            if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
                self._write_reply(
                    process,
                    claude_code_control.deny_line(
                        request_id,
                        "This tool call was denied: "
                        "FineCode could not read the permission request.",
                    ),
                )
                return
            if tool_name in claude_code_questions.disallowed_tool_names(
                self.config.disallowed_tools or []
            ):
                self._write_reply(
                    process,
                    claude_code_control.deny_line(
                        request_id, "This tool is disallowed for this run."
                    ),
                )
                return
            self.logger.info(f"claude asks {tool_name} (request {request_id})")
            async with ask_lock:
                if stop_event.is_set() or entry.withdrawn:
                    return
                if tool_name == "AskUserQuestion":
                    await self._route_question(
                        process, request_id, entry, tool_input, decided_ids
                    )
                else:
                    await self._route_permission(
                        process,
                        request_id,
                        entry,
                        tool_name,
                        tool_input,
                        request,
                        decided_ids,
                    )
        except _QuestionUnavailable as exc:
            stop_event.set()
            for other in pending.values():
                if other.request_id == request_id:
                    continue
                other.withdrawn = True
                if other.task is not None:
                    _ = other.task.cancel()
            queue.put_nowait(("unavailable", exc.question))
        except (asyncio.CancelledError, iprojectactionrunner.ActionRunCancelled) as exc:
            if entry.withdrawn:
                return
            queue.put_nowait(("abandon", exc))
            raise
        finally:
            if pending.pop(request_id, None) is not None and clock is not None:
                clock.changed.set()

    async def _route_question(
        self,
        process: icommandrunner.IAsyncProcess,
        request_id: str,
        entry: _PendingRequest,
        tool_input: dict[str, object],
        decided_ids: set[str],
    ) -> None:
        entries = claude_code_questions.parse_ask_user_question(tool_input)
        if entries is None:
            self._write_reply(
                process,
                claude_code_control.deny_line(
                    request_id, question_support.QUESTION_NOT_SUPPORTED_REPLY
                ),
            )
            if entry.tool_use_id is not None:
                decided_ids.add(entry.tool_use_id)
            return
        outcome = await claude_code_questions.ask_group(self.user_prompt, entries)
        if outcome.end is claude_code_questions.GroupEnd.ANSWERED:
            reply = claude_code_questions.group_reply(outcome, tool_input)
            assert reply["behavior"] == "allow"
            assert isinstance(reply, dict)
            updated = reply.get("updatedInput")
            assert isinstance(updated, dict)
            self._write_reply(
                process,
                claude_code_control.allow_line(request_id, updated),
            )
            return
        if outcome.end is claude_code_questions.GroupEnd.DECLINED:
            reply = claude_code_questions.group_reply(outcome, tool_input)
            assert reply["behavior"] == "deny"
            assert isinstance(reply, dict)
            message = reply.get("message")
            assert isinstance(message, str)
            self._write_reply(
                process,
                claude_code_control.deny_line(request_id, message),
            )
            if entry.tool_use_id is not None:
                decided_ids.add(entry.tool_use_id)
            return
        question = outcome.ending_question or next(
            ask_entry.question for ask_entry in entries
        )
        raise _QuestionUnavailable(question)

    async def _route_permission(
        self,
        process: icommandrunner.IAsyncProcess,
        request_id: str,
        entry: _PendingRequest,
        tool_name: str,
        tool_input: dict[str, object],
        request: typing.Any,
        decided_ids: set[str],
    ) -> None:
        description = request.get("description") if isinstance(request, dict) else None
        end = await claude_code_questions.ask_permission(
            self.user_prompt,
            tool_name,
            tool_input,
            description if isinstance(description, str) else None,
        )
        if end is claude_code_questions.PermissionEnd.ALLOWED:
            self._write_reply(
                process,
                claude_code_control.allow_line(request_id, dict(tool_input)),
            )
            return
        if end is claude_code_questions.PermissionEnd.DENIED:
            self._write_reply(
                process,
                claude_code_control.deny_line(
                    request_id,
                    claude_code_questions.PERMISSION_DENIED_BY_PERSON_REPLY,
                ),
            )
            if entry.tool_use_id is not None:
                decided_ids.add(entry.tool_use_id)
            return
        if end is claude_code_questions.PermissionEnd.DECLINED:
            self._write_reply(
                process,
                claude_code_control.deny_line(
                    request_id, question_support.QUESTION_DECLINED_REPLY
                ),
            )
            if entry.tool_use_id is not None:
                decided_ids.add(entry.tool_use_id)
            return
        raise _QuestionUnavailable(claude_code_questions.permission_question(tool_name))

    def _status(
        self,
        outcome: claude_code_stream.RunResult,
        decided_ids: frozenset[str] = frozenset(),
    ) -> AgentRunStatus:
        """The outcome, distinguishing a refusal to guess from a failure.

        A denial recorded on a run that still succeeded says nothing: the agent
        asked for something, was told no, and found another way. A denial on a
        run that did *not* succeed is the reportable case -- the run needed a
        decision this setup was configured not to make, which is
        `REFUSED_INTERACTION` rather than `FAILED`, and is the distinction a
        non-interactive caller acts on.

        Denials the person decided (or that were never a real question) are
        excluded by tool-use id before that rule; anything claude denied on
        its own still counts.
        """
        if outcome.settled:
            return AgentRunStatus.SETTLED
        if self._undecided_denials(outcome, decided_ids):
            return AgentRunStatus.REFUSED_INTERACTION
        return AgentRunStatus.FAILED

    @staticmethod
    def _undecided_denials(
        outcome: claude_code_stream.RunResult,
        decided_ids: frozenset[str],
    ) -> tuple[str, ...]:
        return tuple(
            denial.tool_name
            for denial in outcome.denials
            if denial.tool_use_id not in decided_ids
        )

    def _error(
        self,
        outcome: claude_code_stream.RunResult,
        decided_ids: frozenset[str] = frozenset(),
    ) -> str | None:
        if outcome.settled:
            return None
        undecided = self._undecided_denials(outcome, decided_ids)
        if not undecided:
            return outcome.error

        denied = ", ".join(sorted(set(undecided)))
        return (
            f"claude was denied permission to use {denied} and no interactive "
            f"channel is available ({outcome.error})"
        )

    async def _finish(
        self,
        process: icommandrunner.IAsyncProcess,
        result: RunAgentTaskRunResult,
        command: list[str],
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
                f"Agent settled without output (exit {exit_code}). Command: {command!r}"
            )

        return result

    async def _abort(self, process: icommandrunner.IAsyncProcess) -> None:
        """Stop claude, escalating until it is actually gone.

        There is no abort message to send: a run without questions closed
        stdin with the prompt, and even a question-mode run's control replies
        cannot stop claude once it is wedged. So the grace
        period is all the politeness available, and after it the ladder has to
        become unconditional -- a claude that outlives its own timeout keeps
        write access to the project a caller may already be restoring and keeps
        spending budget, which is what the timeout exists to bound.

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
                thinking=self.config.thinking,
                settle_timeout_sec=self.config.settle_timeout_sec,
                may_ask_questions=self.config.may_ask_questions,
            )
        resolved = self.config.profiles.get(profile)
        if resolved is None:
            return backend_support.unknown_profile_error(profile, self.config.profiles)
        return _ClaudeRunSettings(
            model=resolved.model if resolved.model is not None else self.config.model,
            thinking=(
                resolved.thinking
                if resolved.thinking is not None
                else self.config.thinking
            ),
            settle_timeout_sec=(
                resolved.settle_timeout_sec
                if resolved.settle_timeout_sec is not None
                else self.config.settle_timeout_sec
            ),
            may_ask_questions=(
                resolved.may_ask_questions
                if resolved.may_ask_questions is not None
                else self.config.may_ask_questions
            ),
        )

    async def _question_start(
        self, payload: RunAgentTaskRunPayload, settings: _ClaudeRunSettings
    ) -> question_support.QuestionStart:
        """The question-mode start decision for an opted-in run."""
        return await question_support.resolve_question_start(
            self.user_prompt,
            opted_in=settings.may_ask_questions,
            start_without_questions=payload.start_without_questions,
            required_shapes=frozenset({QuestionShape.TEXT, QuestionShape.MULTI_CHOICE}),
        )

    def _refused_at_question_start(
        self, payload: RunAgentTaskRunPayload, error: str | None
    ) -> RunAgentTaskRunResult:
        """A refused-at-start result, never spawning claude or probing it."""
        result = question_support.refused_at_start_result(
            error or question_support.START_REFUSAL_ERROR
        )
        if payload.capture_history:
            observation = _pre_session_observation(
                ["refused at the question start check; claude was not started"]
            )
            result = dataclasses.replace(result, observation=observation)
        return result

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
        *,
        continue_session: AgentSessionRef | None = None,
        questions: bool = False,
    ) -> list[str]:
        # `--verbose` is not optional: the CLI rejects `stream-json` output in
        # print mode without it.
        parts = [
            *self._executable(),
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if questions:
            parts += [
                "--input-format",
                "stream-json",
                "--permission-prompt-tool",
                "stdio",
            ]

        if continue_session is not None:
            # Resuming rebuilds the current instructions from these flags;
            # a stale saved snapshot must never stand in for them.
            parts += [
                "--resume",
                continue_session.session_id,
                "--system-prompt-snapshot",
                "off",
            ]
        if settings.model is not None:
            parts += ["--model", settings.model]
        if settings.thinking is not None:
            parts += ["--effort", settings.thinking]
        if self.config.permission_mode is not None:
            parts += ["--permission-mode", self.config.permission_mode]
        if continue_session is not None:
            # A resumed run must name its tool lists explicitly: omitting
            # them would silently restore whatever the saved session allowed.
            parts += ["--allowed-tools", *(self.config.allowed_tools or [""])]
            parts += [
                "--disallowed-tools",
                *(self.config.disallowed_tools or [""]),
            ]
        else:
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

        return parts

    async def _probe_backend_version(self) -> tuple[str | None, str | None]:
        try:
            process = await self.command_runner.run([*self._executable(), "--version"])
        except (OSError, icommandrunner.CommandNotLaunchableError) as error:
            return None, f"claude version probe failed: {error}"
        try:
            await asyncio.wait_for(process.wait_for_end(), timeout=_VERSION_TIMEOUT_SEC)
        except TimeoutError:
            with contextlib.suppress(Exception):
                process.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    process.wait_for_end(), timeout=_SIGNAL_GRACE_SEC
                )
            if process.is_alive():
                with contextlib.suppress(Exception):
                    process.kill()
            return None, "claude version probe timed out"
        except Exception as error:  # noqa: BLE001
            return None, f"claude version probe failed: {error}"
        try:
            if process.get_exit_code() != 0:
                return None, "claude version probe reported a non-zero exit"
            output = (process.get_output() or process.get_error_output() or "").strip()
            if not output:
                return None, "claude version probe produced no output"
            return output.splitlines()[0].strip(), None
        except Exception as error:  # noqa: BLE001
            return None, f"claude version probe failed: {error}"

    async def _attach_observation(
        self,
        result: RunAgentTaskRunResult,
        state: ClaudeObservationState,
    ) -> RunAgentTaskRunResult:
        observation = claude_code_history.locate_claude_history(
            state, claude_code_history.claude_config_root()
        )
        version, version_gap = await self._probe_backend_version()
        gaps = [*observation.gaps]
        if version_gap is not None:
            gaps.append(version_gap)
        observation = dataclasses.replace(
            observation, backend_version=version, gaps=gaps
        )
        return dataclasses.replace(result, observation=observation)
