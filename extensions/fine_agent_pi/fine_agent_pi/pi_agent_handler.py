import asyncio
import contextlib
import dataclasses
import os
import pathlib
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fine_agent import backend_support
from fine_agent.history_types import (
    AgentBackend,
    AgentRunObservation,
    HistoryDiscoveryStatus,
    RelatedHistoryStatus,
    UsageProvenance,
    UsageScope,
    UsageSource,
)
from fine_agent.run_agent_task_action import (
    AgentRunStatus,
    AgentRunUsage,
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
    iprojectinfoprovider,
    iuser_messenger,
)

from fine_agent_pi import pi_history, pi_rpc
from fine_agent_pi.pi_history import PiObservationState

_ABORT_GRACE_SEC = 5.0
"""How long to let pi exit after an abort before giving up on a clean shutdown.

Short on purpose: this runs while the caller is already cancelling or the run
has already been refused, so the only thing still being waited for is pi
noticing. It is not the ER's `_STOP_TIMEOUT_SEC`, which covers a much larger
teardown.
"""

_SIGNAL_GRACE_SEC = 2.0
"""How long each signal in the teardown ladder gets before the next one.

Shorter than the abort grace: by this point pi has already declined a clean
stop, so what is being waited for is the kernel delivering a signal, not an
agent winding down.
"""

_STATS_TIMEOUT_SEC = 2.0
"""How long to wait for pi's session totals once the work itself is done.

Short, and failure here is silent: this is accounting collected after the run
has already produced its answer, so a pi that will not report must cost the
caller a couple of seconds, never the result.
"""

_VERSION_TIMEOUT_SEC = 3.0
"""Bound for the post-run `--version` probe on captured runs."""

_POLICY_ABORT = "abort"
_POLICY_CANCEL = "cancel"


@dataclasses.dataclass
class PiAgentProfile:
    """Per-role overrides for one named agent run. A `None` field inherits the
    handler's top-level value."""

    model: str | None = None
    provider: str | None = None
    thinking: str | None = None
    settle_timeout_sec: float | None = None


@dataclasses.dataclass
class PiAgentHandlerConfig(code_action.ActionHandlerConfig):
    model: str | None = None
    """Model pattern passed to `pi --model`. `None` leaves pi's own default.

    Lives here rather than on the payload so the same task definition runs
    against a different model without being edited (PRD-0005 R3).
    """
    provider: str | None = None
    thinking: str | None = None
    """Thinking level passed to `pi --thinking` (`off`, `minimal`, `low`,
    `medium`, `high`, `xhigh`, `max`). `None` leaves pi's own default."""
    ui_policy: dict[str, str] = dataclasses.field(default_factory=dict)
    """UI method (`select`/`confirm`/`input`/`editor`) -> how to answer it.

    `abort` refuses and ends the run, `cancel` declines and lets the agent
    continue, and any other string is sent back as the literal answer. Keyed by
    method name rather than being a list so an override is expressible in an
    environment variable (S-205).

    `confirm` is the exception: it takes a yes/no word (`yes`/`no`/`true`/
    `false`/`1`/`0`/`allow`/`deny`, case-insensitive), because its reply field
    is a boolean and there is no literal answer to pass through. A `confirm`
    policy that is none of those refuses the dialog and ends the run rather
    than guessing which way it was meant.
    """
    default_ui_policy: str = _POLICY_ABORT
    """Applied to methods absent from `ui_policy`. Defaults to refusing, so a
    setup that has not thought about interaction stops rather than guessing an
    answer on the user's behalf."""
    settle_timeout_sec: float = 900.0
    """Ceiling on one agent run. An agent loop has no natural bound, so without
    this a wedged run never ends."""
    profiles: dict[str, PiAgentProfile] = dataclasses.field(default_factory=dict)
    """Named runs, keyed by the `profile` a caller passes, so an override can be
    expressed in an environment variable (S-205).

    A field left `None` on a profile inherits the top-level value; a profile
    therefore cannot ask for pi's own default for a field the top level sets.
    Later knobs (tools, ask-user policy, budget) must choose inherit-or-empty
    deliberately per field -- inheriting a permission set can grant more than
    the profile intended.
    """


@dataclasses.dataclass(frozen=True)
class _PiRunSettings:
    """The pi run knobs for one run, after a profile is resolved.

    Separated from the config so `_build_command` and the timeout depend on the
    run's resolved values rather than on whichever config object happened to be
    in scope.
    """

    model: str | None
    provider: str | None
    thinking: str | None
    settle_timeout_sec: float


@dataclasses.dataclass
class _PiRunMetadata:
    """Task-owned identity evidence collected across the run's readers.

    One `get_state` command is sent per run; its correlated response may arrive
    during the main event loop or late while awaiting session stats. Every
    reader records into this holder so no arrival order erases evidence: a
    timeout never clears stats already recorded here.
    """

    identity_request_id: str
    identity: pi_rpc.PiSessionIdentity | None = None
    stats_received: bool = False
    stats: pi_rpc.PiSessionStats | None = None


def _stats_provenance() -> UsageProvenance:
    return UsageProvenance(
        token_source=UsageSource.PI_STATS,
        token_scope=UsageScope.SESSION,
        cost_source=UsageSource.PI_STATS,
        cost_scope=UsageScope.SESSION,
    )


def _message_provenance() -> UsageProvenance:
    return UsageProvenance(
        token_source=UsageSource.PI_MESSAGES,
        token_scope=UsageScope.MESSAGE_SUM,
        cost_source=UsageSource.PI_MESSAGES,
        cost_scope=UsageScope.MESSAGE_SUM,
    )


def _unknown_provenance() -> UsageProvenance:
    return UsageProvenance(
        token_source=UsageSource.UNKNOWN,
        token_scope=UsageScope.UNKNOWN,
        cost_source=UsageSource.UNKNOWN,
        cost_scope=UsageScope.UNKNOWN,
    )


def _pre_session_observation(gaps: list[str]) -> AgentRunObservation:
    return AgentRunObservation(
        backend=AgentBackend.PI,
        backend_version=None,
        session_id=None,
        history_root=None,
        history_status=HistoryDiscoveryStatus.UNAVAILABLE,
        files=[],
        related_status=RelatedHistoryStatus.UNAVAILABLE,
        usage_provenance=_unknown_provenance(),
        gaps=list(gaps),
    )


def _native_root_for(session_file: str | None) -> pathlib.Path:
    if session_file:
        candidate = pathlib.Path(session_file)
        if candidate.is_absolute():
            return candidate.parent
    return pi_history.pi_sessions_root()


def _same_path(left: str, right: pathlib.Path) -> bool:
    """Whether a reported session file is the looked-up native file.

    Lexical only: no filesystem access, so symlinks keep the project identity
    the caller spelled and the check stays usable on the async confirmation
    path without blocking the loop.
    """
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(str(right))
    )


def _sessions_equal(left: str, right: str) -> bool:
    """Whether two session IDs name the same UUID value."""
    try:
        return uuid.UUID(left) == uuid.UUID(right)
    except ValueError:
        return left == right


def _merged_session_identity(
    stats_info: pi_rpc.PiSessionStats | None,
    metadata: _PiRunMetadata,
) -> tuple[str | None, str | None, str | None]:
    """The reported session ID/file, preferring stats, else the state check.

    Returns the merged ID, the merged file, and a conflict reason. Both
    sources reporting different IDs rejects the settlement: neither is handed
    out as the run's session. Identity stays independent of usage: missing
    usage never fails a run with reported identity, and missing identity is
    left for the settled-ID rule to report.
    """
    stats_id = stats_info.session_id if stats_info is not None else None
    stats_file = stats_info.session_file if stats_info is not None else None
    identity = metadata.identity
    state_ok = (
        identity is not None
        and identity.request_id == metadata.identity_request_id
        and identity.error is None
        and identity.session_id is not None
    )
    state_id = identity.session_id if state_ok and identity is not None else None
    state_file = identity.session_file if state_ok and identity is not None else None
    if stats_id and state_id and not _sessions_equal(stats_id, state_id):
        return (
            None,
            None,
            (
                f"pi reported conflicting session identities {stats_id!r} and "
                f"{state_id!r}; no session reference is available"
            ),
        )
    if stats_id:
        return stats_id, stats_file, None
    if state_id:
        return state_id, state_file, None
    return None, None, None


def _merged_state(
    usage: AgentRunUsage | None,
    stats_info: pi_rpc.PiSessionStats | None,
    session_id: str | None,
    session_file: str | None,
) -> PiObservationState:
    """Observation state for a settled run with merged session identity."""
    base = _state_from_settle(usage, stats_info)
    return PiObservationState(
        session_id=session_id,
        session_file=session_file,
        usage_provenance=base.usage_provenance,
        gaps=list(base.gaps),
    )


def _state_from_settle(
    usage: AgentRunUsage | None,
    stats_info: pi_rpc.PiSessionStats | None,
) -> PiObservationState:
    if stats_info is not None:
        return PiObservationState(
            session_id=stats_info.session_id,
            session_file=stats_info.session_file,
            usage_provenance=_stats_provenance(),
            gaps=[],
        )
    if usage is not None:
        return PiObservationState(
            session_id=None,
            session_file=None,
            usage_provenance=_message_provenance(),
            gaps=["pi reported no session stats; using message totals"],
        )
    return PiObservationState(
        session_id=None,
        session_file=None,
        usage_provenance=_unknown_provenance(),
        gaps=["pi reported no session stats; using message totals"],
    )


class PiAgentHandler(
    code_action.ActionHandler[
        RunAgentTaskAction,
        PiAgentHandlerConfig,
    ]
):
    """Run a task with the pi coding agent, driving it over its RPC mode.

    pi picks one mode per process, and RPC is JSONL over that process's
    stdin/stdout, so this handler owns the pipe for the run's whole duration and
    forwards what it sees into FineCode's progress and message surfaces.

    Exit codes: pi does not document them. Non-zero is treated as failure with
    stderr attached, but `0` is *not* sufficient for success -- a run whose
    model call was rejected outright still settles and still exits `0`, so the
    outcome is decided from the stream (`_drive`) and the exit code only adds
    to it. Codes observed in practice should be recorded here as they are found
    -- until then the handling is deliberately coarse rather than pretending to
    a precision it does not have (R-501).

    Text snapshots flush at each assistant ``message_end`` and at most once per
    2 seconds while deltas arrive. For M assistant messages over T seconds, at
    most M + ceil(T / 2) + 1 partials are sent. Snapshot text across those hops
    is bounded by that count times the longest snapshot; terminal fields are
    not part of this bound.
    """

    def __init__(
        self,
        config: PiAgentHandlerConfig,
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
        snapshots = SnapshotStream(
            run_context.partial_result_sender,
            min_interval_sec=2.0,
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
        # Every run persists: pi needs a session file whether or not the caller
        # asked to observe it. The requested ID is only ever evidence of what
        # was asked for, never proof of what pi reported.
        requested_session_id = str(uuid.uuid4())
        command = self._build_command(settings, requested_session_id)
        project_dir = self.project_info_provider.get_current_project_dir_path()
        self.logger.debug(f"Starting pi: {command!r} in {project_dir}")

        started = time.monotonic()
        try:
            process = await self.command_runner.start_long_running(
                command,
                cwd=project_dir,
                # pi runs tools of its own, so tearing it down has to reach them
                # too -- otherwise an abandoned run leaves a subtree editing the
                # project after this handler has returned.
                new_process_group=True,
            )
        except icommandrunner.CommandStartError as error:
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
            requested_session_id,
            snapshots,
        )

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
            failure = session_backend_error(ref, AgentBackend.PI)
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

        found_id, found_path, gaps = await asyncio.to_thread(
            pi_history.find_pi_session_file,
            pi_history.pi_sessions_root(),
            ref.session_id,
            None,
        )
        if (
            found_path is None
            or found_id is None
            or not _sessions_equal(found_id, ref.session_id)
        ):
            reason = "; ".join(gaps) or (f"pi session {ref.session_id!r} was not found")
            return self._continuation_failure(
                payload,
                reason,
                started,
                observation=_pre_session_observation([reason]),
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
                f"cannot continue pi session {ref.session_id!r} without an "
                f"explicit model: the handler cannot reset the saved session "
                f"model to the native new-run default; set 'model' in "
                f"{profile_name}",
                started,
            )

        command = self._build_command(settings, ref.session_id, continuing=True)
        self.logger.debug(f"Continuing pi: {command!r} in {project_dir}")

        try:
            process = await self.command_runner.start_long_running(
                command,
                cwd=project_dir,
                new_process_group=True,
            )
        except icommandrunner.CommandStartError as error:
            return self._continuation_failure(
                payload,
                backend_support.spawn_error(command[0], error),
                started,
            )

        return await self._settle_spawned_run(
            payload,
            run_context,
            process,
            command,
            settings,
            project_dir,
            started,
            None,
            snapshots,
            expected_session=ref,
            expected_session_file=found_path,
        )

    async def _settle_spawned_run(
        self,
        payload: RunAgentTaskRunPayload,
        run_context: RunAgentTaskRunContext,
        process: icommandrunner.IAsyncProcess,
        command: list[str],
        settings: _PiRunSettings,
        project_dir: pathlib.Path,
        started: float,
        requested_session_id: str | None,
        snapshots: SnapshotStream,
        *,
        expected_session: AgentSessionRef | None = None,
        expected_session_file: pathlib.Path | None = None,
    ) -> RunAgentTaskRunResult:
        try:
            result, observation_state = await self._run_with_process(
                payload,
                run_context,
                process,
                command,
                settings,
                snapshots,
                expected_session=expected_session,
                expected_session_file=expected_session_file,
            )
        except BaseException:
            # Anything escaping the drive that the paths inside do not already
            # handle -- a stream that failed its size limit or its encoding
            # (`stdout_lines` raises `RuntimeError`), a bug in a frame handler.
            # Without this, pi is left with stdin open and blocks on it forever,
            # never exiting. The teardown is the same one every other exit path uses,
            # and `_abort` is idempotent, so running it again after one of those
            # costs nothing.
            await self._abort(process)
            raise

        # Stamped on the way out of every path rather than at each construction
        # site inside `_drive`: all of them sit within the window being
        # measured, and elapsed time is as much a part of a failed run's account
        # as of a settled one's.
        result.duration_sec = time.monotonic() - started
        # Finalize the session after existing structured-output, exit and
        # cleanup processing. A settled run without reported identity fails
        # here with its raw output kept; failed/refused runs keep their
        # existing outcome.
        result = with_session(
            result,
            backend=AgentBackend.PI,
            project_dir=project_dir,
            reported_id=observation_state.session_id,
            expected=expected_session,
        )
        if payload.capture_history:
            try:
                result = await self._attach_observation(
                    result, observation_state, requested_session_id, expected_session
                )
            except Exception as error:  # noqa: BLE001
                gaps = [*observation_state.gaps, f"observation failed: {error}"]
                self.logger.debug(f"pi observation failed: {error}")
                failed = AgentRunObservation(
                    backend=AgentBackend.PI,
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

    async def _run_with_process(
        self,
        payload: RunAgentTaskRunPayload,
        run_context: RunAgentTaskRunContext,
        process: icommandrunner.IAsyncProcess,
        command: list[str],
        settings: _PiRunSettings,
        snapshots: SnapshotStream,
        *,
        expected_session: AgentSessionRef | None = None,
        expected_session_file: pathlib.Path | None = None,
    ) -> tuple[RunAgentTaskRunResult, PiObservationState]:
        prompt = payload.prompt
        if payload.output_schema is not None:
            # Appended, not substituted: the task wording is the caller's, and
            # the output instruction is the only thing the backend adds.
            prompt += backend_support.json_output_instruction(payload.output_schema)
        metadata = _PiRunMetadata(identity_request_id=f"get-state-{uuid.uuid4().hex}")

        async with run_context.progress("Agent task", cancellable=True) as progress:
            try:
                result, observation_state = await asyncio.wait_for(
                    self._drive(
                        process,
                        prompt,
                        progress,
                        metadata,
                        snapshots,
                        expected_session=expected_session,
                        expected_session_file=expected_session_file,
                    ),
                    timeout=settings.settle_timeout_sec,
                )
            except asyncio.CancelledError:
                # The caller withdrew. Tell pi before the process is torn down,
                # so it can stop its own children rather than being orphaned.
                #
                # Nothing is returned on this path, so whatever the run spent
                # before being cancelled is lost with it -- the same reason the
                # `ABORTED` status is never produced either.
                await self._abort(process)
                raise
            except TimeoutError:
                await self._abort(process)
                timed_out = RunAgentTaskRunResult(
                    status=AgentRunStatus.FAILED,
                    error=(f"pi did not settle within {settings.settle_timeout_sec}s"),
                )
                timed_out_state = PiObservationState(
                    session_id=None,
                    session_file=None,
                    usage_provenance=_unknown_provenance(),
                    gaps=["pi did not settle; session stats were not collected"],
                )
                return timed_out, timed_out_state

        finished = await self._finish(process, result, command)
        return self._extract_structured_output(payload, finished), observation_state

    def _extract_structured_output(
        self,
        payload: RunAgentTaskRunPayload,
        result: RunAgentTaskRunResult,
    ) -> RunAgentTaskRunResult:
        """Decode the fenced JSON block a settled run was asked to end with.

        Only a settled run is read: a failed one already has a more specific
        error, and looking for JSON in a truncated answer would replace it with
        a misleading "no block". `replace` keeps `usage`, `turns` and `output`
        on both the success and the failure path, so a run that failed on its
        answer still accounts for what it spent.
        """
        if payload.output_schema is None or result.status is not AgentRunStatus.SETTLED:
            return result
        try:
            value = backend_support.extract_last_json_block(result.output)
        except backend_support.StructuredOutputError as error:
            return dataclasses.replace(
                result, status=AgentRunStatus.FAILED, error=str(error)
            )
        return dataclasses.replace(result, structured_output=value)

    async def _drive(
        self,
        process: icommandrunner.IAsyncProcess,
        prompt: str,
        progress: code_action.ProgressContext,
        metadata: _PiRunMetadata,
        snapshots: SnapshotStream,
        *,
        expected_session: AgentSessionRef | None = None,
        expected_session_file: pathlib.Path | None = None,
    ) -> tuple[RunAgentTaskRunResult, PiObservationState]:
        # One subscription for the whole run: confirmation (on continuation)
        # and the main loop below share these same lines. A second subscriber
        # is not allowed (`IAsyncProcess.stdout_lines` supports only one).
        lines = process.stdout_lines()
        if expected_session is not None:
            confirmed = await self._confirm_session(
                process,
                lines,
                metadata,
                expected_session,
                expected_session_file,
                progress,
            )
            if isinstance(confirmed, RunAgentTaskRunResult):
                state = PiObservationState(
                    session_id=None,
                    session_file=None,
                    usage_provenance=_unknown_provenance(),
                    gaps=[confirmed.error or "pi session confirmation failed"],
                )
                return confirmed, state
        else:
            # A new run sends its identity check first and its prompt
            # immediately afterward; the correlated response is collected in
            # the ordinary event loop below, without delaying the prompt.
            try:
                process.write_to_stdin(
                    pi_rpc.session_state_command(metadata.identity_request_id)
                )
                process.write_to_stdin(pi_rpc.prompt_command(prompt))
            except (BrokenPipeError, ConnectionResetError) as error:
                state = PiObservationState(
                    session_id=None,
                    session_file=None,
                    usage_provenance=_unknown_provenance(),
                    gaps=[f"pi closed its prompt pipe before reading it: {error}"],
                )
                return RunAgentTaskRunResult(
                    status=AgentRunStatus.FAILED,
                    output="",
                    error=f"pi closed its prompt pipe before reading it: {error}",
                ), state
            # The prompt is already sent for new runs; skip the shared send.
            return await self._drive_events(
                process, lines, progress, metadata, snapshots
            )
        try:
            process.write_to_stdin(pi_rpc.prompt_command(prompt))
        except (BrokenPipeError, ConnectionResetError) as error:
            state = PiObservationState(
                session_id=None,
                session_file=None,
                usage_provenance=_unknown_provenance(),
                gaps=[f"pi closed its prompt pipe before reading it: {error}"],
            )
            return RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED,
                output="",
                error=f"pi closed its prompt pipe before reading it: {error}",
            ), state
        return await self._drive_events(process, lines, progress, metadata, snapshots)

    async def _confirm_session(
        self,
        process: icommandrunner.IAsyncProcess,
        lines: AsyncIterator[str],
        metadata: _PiRunMetadata,
        expected_session: AgentSessionRef,
        expected_session_file: pathlib.Path | None,
        progress: code_action.ProgressContext,
    ) -> pi_rpc.PiSessionIdentity | RunAgentTaskRunResult:
        """Confirm the resumed session before the task prompt is sent.

        Consumes the shared stdout iterator, never a second subscription.
        Returns the confirmed identity on success, a typed result with
        `FAILED` for startup failure or `REFUSED_INTERACTION` for UI refusal.
        Cancellation propagates and returns neither. EOF, failure, missing
        identity or a different UUID all fail before the prompt is sent: no
        prompt is ever sent to test whether startup succeeded.
        """
        await progress.report("Confirming session")
        try:
            process.write_to_stdin(
                pi_rpc.session_state_command(metadata.identity_request_id)
            )
        except (BrokenPipeError, ConnectionResetError) as error:
            await self._abort(process)
            return RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED,
                output="",
                error=f"pi closed its prompt pipe before reading it: {error}",
            )
        async for line in lines:
            frame = pi_rpc.parse_frame(line)
            if frame is None:
                self.logger.debug("Ignoring non-JSON line from pi")
                continue
            identity = pi_rpc.parse_session_identity(frame)
            if identity is not None:
                if identity.request_id != metadata.identity_request_id:
                    continue
                metadata.identity = identity
                if identity.error is not None:
                    await self._abort(process)
                    return RunAgentTaskRunResult(
                        status=AgentRunStatus.FAILED,
                        output="",
                        error=(
                            "pi session confirmation failed for "
                            f"{expected_session.session_id!r}: {identity.error}"
                        ),
                    )
                if identity.session_id is None or not _sessions_equal(
                    identity.session_id, expected_session.session_id
                ):
                    await self._abort(process)
                    return RunAgentTaskRunResult(
                        status=AgentRunStatus.FAILED,
                        output="",
                        error=(
                            "pi resumed unexpected session "
                            f"{identity.session_id!r}, expected "
                            f"{expected_session.session_id!r}"
                        ),
                    )
                if (
                    expected_session_file is not None
                    and identity.session_file is not None
                    and not _same_path(identity.session_file, expected_session_file)
                ):
                    await self._abort(process)
                    return RunAgentTaskRunResult(
                        status=AgentRunStatus.FAILED,
                        output="",
                        error=(
                            "pi resumed unexpected session "
                            f"{identity.session_id!r}, expected "
                            f"{expected_session.session_id!r}"
                        ),
                    )
                return identity
            if frame.get("type") == pi_rpc.PiEvent.EXTENSION_UI_REQUEST:
                refused = await self._answer_ui_request(process, frame)
                if refused is not None:
                    await self._abort(process)
                    return RunAgentTaskRunResult(
                        status=AgentRunStatus.REFUSED_INTERACTION,
                        output="",
                        error=refused,
                    )
                continue
        await self._abort(process)
        return RunAgentTaskRunResult(
            status=AgentRunStatus.FAILED,
            output="",
            error=(
                f"pi stopped without confirming session {expected_session.session_id!r}"
            ),
        )

    async def _drive_events(
        self,
        process: icommandrunner.IAsyncProcess,
        lines: AsyncIterator[str],
        progress: code_action.ProgressContext,
        metadata: _PiRunMetadata,
        snapshots: SnapshotStream,
    ) -> tuple[RunAgentTaskRunResult, PiObservationState]:

        output: list[str] = []
        turns = 0
        usage: AgentRunUsage | None = None
        """Running total from the message frames.

        `None` until pi reports something, so a run that reported nothing stays
        distinguishable from one that reported zeros. Superseded by the session
        totals when the run settles and pi answers -- this accumulation exists
        for the paths where it never gets that far.
        """
        stats_info: pi_rpc.PiSessionStats | None = None
        refused: str | None = None
        settled = False
        failure: str | None = None
        """The provider error from the most recent turn, if it failed.

        Cleared at each `turn_start`, so an error that pi went on to retry
        successfully does not condemn a run that recovered -- only an error
        left standing when the stream ends is the run's outcome.
        """

        async for line in lines:
            frame = pi_rpc.parse_frame(line)
            if frame is None:
                # Strict JSONL is documented, but a banner on stdout must not
                # end a run that is otherwise fine.
                self.logger.debug(f"Ignoring non-JSON line from pi: {line[:200]}")
                continue

            event = frame.get("type")

            if event == pi_rpc.PiEvent.AGENT_SETTLED:
                settled = True
                usage, stats_info = await self._read_session_stats(
                    process, lines, usage, metadata
                )
                break

            if event == pi_rpc.PiEvent.AGENT_END:
                # NOT the end of the run. `agent_end` fires per low-level run
                # and sets `willRetry` when pi will try again after a transient
                # error; only `agent_settled` means nothing further is pending.
                if pi_rpc.will_retry(frame):
                    await progress.report("Retrying after a transient error")
                continue

            if event == pi_rpc.PiEvent.MESSAGE_UPDATE:
                delta = pi_rpc.text_delta(frame)
                if delta is not None:
                    output.append(delta)
                    await snapshots.update("".join(output))
                continue

            if event in (pi_rpc.PiEvent.MESSAGE_END, pi_rpc.PiEvent.TURN_END):
                # Both frames carry the same error for a failed turn. Reading
                # both rather than only `turn_end` keeps the reason available
                # when pi dies between the two.
                error = pi_rpc.message_error(frame)
                if error is not None:
                    failure = error

                if event == pi_rpc.PiEvent.MESSAGE_END:
                    # Usage, unlike the error, is read from one of the two only.
                    # They carry the same message, so accumulating from both
                    # doubles every figure -- including the reported bill.
                    reported = pi_rpc.message_usage(frame)
                    if reported is not None:
                        usage = reported if usage is None else usage.combine(reported)
                    message = frame.get("message")
                    if isinstance(message, dict) and message.get("role") == "assistant":
                        await snapshots.update("".join(output), message_end=True)
                continue

            if event == pi_rpc.PiEvent.TURN_START:
                turns += 1
                failure = None
                await progress.report(f"Turn {turns}")
                continue

            if event == pi_rpc.PiEvent.TOOL_EXECUTION_START:
                await progress.report(f"Tool: {frame.get('toolName') or 'unknown'}")
                continue

            if event == pi_rpc.PiEvent.COMPACTION_START:
                await progress.report("Compacting context")
                continue

            if event == pi_rpc.PiEvent.AUTO_RETRY_START:
                await progress.report("Waiting to retry")
                continue

            if event == pi_rpc.PiEvent.RESPONSE:
                identity = pi_rpc.parse_session_identity(frame)
                if identity is not None:
                    # The startup identity check answers asynchronously: it may
                    # arrive here, long after it was sent, or late while the
                    # stats reader below is waiting. Recorded, never acted on
                    # directly; settlement merges it with the stats identity.
                    if identity.request_id == metadata.identity_request_id:
                        metadata.identity = identity
                    continue
                if not frame.get("success", True):
                    self.logger.warning(f"pi rejected a command: {frame}")
                continue

            if event == pi_rpc.PiEvent.EXTENSION_UI_REQUEST:
                refused = await self._answer_ui_request(process, frame)
                if refused is not None:
                    await self._abort(process)
                    break

        if refused is not None:
            state = PiObservationState(
                session_id=stats_info.session_id if stats_info else None,
                session_file=stats_info.session_file if stats_info else None,
                usage_provenance=_message_provenance()
                if usage is not None
                else _unknown_provenance(),
                gaps=["no native session was available"],
            )
            return RunAgentTaskRunResult(
                status=AgentRunStatus.REFUSED_INTERACTION,
                output="".join(output),
                turns=turns,
                usage=usage,
                error=refused,
            ), state

        if not settled:
            # The stream reached EOF without `agent_settled`: pi died, or was
            # killed, mid-run. Whatever text arrived first is kept -- it is
            # still the best account of what happened -- but reporting SETTLED
            # here would tell the caller a truncated run finished its work.
            reason = "pi stopped without settling"
            state = PiObservationState(
                session_id=None,
                session_file=None,
                usage_provenance=_message_provenance()
                if usage is not None
                else _unknown_provenance(),
                gaps=["pi reported no session stats; using message totals"],
            )
            return RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED,
                output="".join(output),
                turns=turns,
                usage=usage,
                error=f"{failure}; {reason}" if failure else reason,
            ), state

        if failure is not None:
            # Settled, but the last turn never reached the model. pi reports
            # this only in the message frames and still exits 0, so trusting
            # `agent_settled` alone reports a provider outage -- an expired
            # key, an unfunded account, a rate limit -- as a successful run
            # that happened to say nothing.
            state = _state_from_settle(usage, stats_info)
            return RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED,
                output="".join(output),
                turns=turns,
                usage=usage,
                error=failure,
            ), state

        merged_id, merged_file, conflict = _merged_session_identity(
            stats_info, metadata
        )
        if conflict is not None:
            state = _state_from_settle(usage, stats_info)
            return RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED,
                output="".join(output),
                turns=turns,
                usage=usage,
                error=conflict,
            ), state
        state = _merged_state(usage, stats_info, merged_id, merged_file)
        return RunAgentTaskRunResult(
            status=AgentRunStatus.SETTLED,
            output="".join(output),
            turns=turns,
            usage=usage,
        ), state

    async def _read_session_stats(
        self,
        process: icommandrunner.IAsyncProcess,
        lines: AsyncIterator[str],
        accumulated: AgentRunUsage | None,
        metadata: _PiRunMetadata,
    ) -> tuple[AgentRunUsage | None, pi_rpc.PiSessionStats | None]:
        """Ask pi for the session totals, falling back to `accumulated`.

        pi's totals are the better number: they also cover what the agent's own
        tools and its context compaction spent, neither of which ever reaches
        the message frames this handler sums. But they are only obtainable
        while pi is still alive and answering, so the summed figure remains the
        answer on every path that does not reach a settle.

        Best-effort throughout. This runs after the run has already produced
        its output, so no failure here may change what is returned. A timeout
        never erases stats already recorded in the holder: what is there is
        still used, and the correlated identity response is recognized here
        too, covering a state response that arrives after the stats response.
        """
        with contextlib.suppress(Exception):
            process.write_to_stdin(pi_rpc.session_stats_command())

        stats: pi_rpc.PiSessionStats | None = None
        with contextlib.suppress(TimeoutError):
            stats = await asyncio.wait_for(
                self._await_session_stats(lines, metadata), timeout=_STATS_TIMEOUT_SEC
            )
        if stats is None and metadata.stats is not None:
            stats = metadata.stats

        if stats is None or stats.usage is None:
            self.logger.debug("pi reported no session stats; using message totals")
            return accumulated, stats

        # The stats response names no provider or model, so the identity comes
        # from the messages. Reattached rather than dropped because a cost with
        # nothing to attribute it to cannot be compared against anything.
        reattached = dataclasses.replace(
            stats.usage,
            provider=accumulated.provider if accumulated else None,
            model=accumulated.model if accumulated else None,
        )
        return reattached, stats

    async def _await_session_stats(
        self, lines: AsyncIterator[str], metadata: _PiRunMetadata
    ) -> pi_rpc.PiSessionStats | None:
        """Read frames until the stats response arrives, or the stream ends.

        Consuming the caller's own iterator from inside its loop body is safe:
        the outer `async for` is suspended between items rather than running,
        and it breaks out immediately after this returns. Abandoning the stream
        here -- which is what a timeout does -- is likewise harmless for the
        same reason, and `_finish` reads only stderr and the exit code.
        """
        async for line in lines:
            frame = pi_rpc.parse_frame(line)
            if frame is None:
                continue
            if frame.get("type") == pi_rpc.PiEvent.RESPONSE and (
                pi_rpc.is_session_stats_response(frame)
            ):
                stats = pi_rpc.parse_session_stats(frame)
                metadata.stats = stats
                metadata.stats_received = True
                # Finished once the stats response arrived alongside usable
                # identity evidence: a usable stats ID, or the correlated
                # state response. Otherwise keep waiting for the identity to
                # arrive until the overall stats timeout bounds the wait.
                if (stats is not None and stats.session_id) or (
                    metadata.identity is not None
                    and metadata.identity.request_id == metadata.identity_request_id
                    and metadata.identity.error is None
                    and metadata.identity.session_id
                ):
                    return stats
                continue
            identity = pi_rpc.parse_session_identity(frame)
            if identity is not None:
                if identity.request_id == metadata.identity_request_id:
                    metadata.identity = identity
                if metadata.stats_received:
                    stats = metadata.stats
                    if (stats is not None and stats.session_id) or (
                        identity.request_id == metadata.identity_request_id
                        and identity.error is None
                        and identity.session_id
                    ):
                        return stats
                continue
        return metadata.stats

    async def _answer_ui_request(
        self,
        process: icommandrunner.IAsyncProcess,
        frame: dict[str, Any],
    ) -> str | None:
        """Answer one UI request. Returns a refusal reason, or `None` to continue.

        Every request that expects a reply gets one. Letting a request lapse is
        not a neutral act: pi auto-resolves it with its own default once
        `timeout` expires, silently, which for a tool-approval gate is an
        unlogged auto-approval.
        """
        request = pi_rpc.parse_ui_request(frame)
        if request is None:
            # No id: a reply is addressed by it, so there is nothing that can be
            # sent. The only frame this integration is unable to decline.
            self.logger.warning(
                f"Unanswerable extension_ui_request from pi (no id): {frame}"
            )
            return None

        if request.disposition is pi_rpc.UiDisposition.IGNORE:
            # notify/setStatus/setWidget/... are fire-and-forget; replying to
            # one is not harmless, it is an unmatched id.
            self.logger.debug(f"pi UI notice ({request.method}): {request.title}")
            return None

        if request.disposition is pi_rpc.UiDisposition.UNKNOWN:
            # Fail closed. Staying silent would let pi's own timeout resolve an
            # interaction this integration does not understand, with a default
            # nobody chose -- the exact silent auto-answer the policy exists to
            # prevent.
            self.logger.warning(f"Unknown pi UI method {request.method!r}; refusing")
            process.write_to_stdin(pi_rpc.ui_response(request, cancelled=True))
            described = request.method or "no method given"
            return f"pi asked an unsupported question ({described})"

        policy = self.config.ui_policy.get(
            request.method, self.config.default_ui_policy
        )

        self.logger.info(
            f"Answering pi UI request ({request.method}) "
            f"{request.title!r} with policy {policy!r}"
        )

        if policy == _POLICY_ABORT:
            process.write_to_stdin(pi_rpc.ui_response(request, cancelled=True))
            return (
                f"pi asked the user ({request.method}: {request.title!r}) and no "
                "interactive channel is available"
            )

        if policy == _POLICY_CANCEL:
            process.write_to_stdin(pi_rpc.ui_response(request, cancelled=True))
            return None

        if request.method == pi_rpc.UiMethod.CONFIRM:
            # A confirm has no free-text answer: the policy string is a yes or a
            # no, and anything else is a misconfiguration. Fail closed rather
            # than pick a side -- confirm is what pi asks before acting.
            answer = pi_rpc.parse_confirm_answer(policy)
            if answer is None:
                self.logger.warning(
                    f"confirm policy {policy!r} is not a yes/no answer; refusing"
                )
                process.write_to_stdin(pi_rpc.ui_response(request, cancelled=True))
                return (
                    f"pi asked for confirmation ({request.title!r}) and the "
                    f"configured policy {policy!r} is not a yes/no answer"
                )
            process.write_to_stdin(
                pi_rpc.ui_response(request, cancelled=False, value=answer)
            )
            return None

        process.write_to_stdin(
            pi_rpc.ui_response(request, cancelled=False, value=policy)
        )
        return None

    async def _finish(
        self,
        process: icommandrunner.IAsyncProcess,
        result: RunAgentTaskRunResult,
        command: list[str],
    ) -> RunAgentTaskRunResult:
        with contextlib.suppress(TimeoutError):
            process.close_stdin()
            await process.wait_for_end(timeout=_ABORT_GRACE_SEC)

        exit_code = process.get_exit_code()
        stderr = process.get_error_output()
        self.logger.debug(f"pi exited with {exit_code}; stderr: {stderr}")

        if exit_code not in (0, None) and result.status is not (
            AgentRunStatus.REFUSED_INTERACTION
        ):
            # A refusal already explains itself, and aborting pi is expected to
            # produce a non-zero exit -- reporting that instead would replace
            # the real reason with a symptom of our own teardown.
            reason = f"pi exited with code {exit_code}: {stderr.strip()}"
            return RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED,
                output=result.output,
                turns=result.turns,
                usage=result.usage,
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
        """Stop pi, escalating until it is actually gone.

        Asking first is worth the grace period: pi runs tools of its own, and
        the polite exit is the one that lets it stop them. But asking is not
        enough on its own -- a pi that ignores `abort` keeps write access to the
        project and keeps spending, which is what the timeout exists to bound,
        so the ladder ends somewhere unconditional.

        Best-effort throughout: this runs on paths where something has already
        gone wrong or been withdrawn, and a failure to abort cleanly must not
        replace the original outcome with a teardown error.
        """
        with contextlib.suppress(Exception):
            process.write_to_stdin(pi_rpc.abort_command())
        with contextlib.suppress(Exception):
            process.close_stdin()
        with contextlib.suppress(Exception):
            await process.wait_for_end(timeout=_ABORT_GRACE_SEC)

        if not process.is_alive():
            return

        self.logger.warning(f"pi ignored abort for {_ABORT_GRACE_SEC}s; terminating it")
        with contextlib.suppress(Exception):
            process.terminate()
        with contextlib.suppress(Exception):
            await process.wait_for_end(timeout=_SIGNAL_GRACE_SEC)

        if not process.is_alive():
            return

        self.logger.warning("pi ignored SIGTERM; killing it")
        with contextlib.suppress(Exception):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait_for_end(timeout=_SIGNAL_GRACE_SEC)

    def _resolve(self, profile: str | None) -> _PiRunSettings | str:
        """The run settings for *profile*, or an error message to fail with.

        `None` is the top-level settings, so an existing caller that sends no
        profile gets exactly today's command.
        """
        if profile is None:
            return _PiRunSettings(
                model=self.config.model,
                provider=self.config.provider,
                thinking=self.config.thinking,
                settle_timeout_sec=self.config.settle_timeout_sec,
            )
        resolved = self.config.profiles.get(profile)
        if resolved is None:
            return backend_support.unknown_profile_error(profile, self.config.profiles)
        return _PiRunSettings(
            model=resolved.model if resolved.model is not None else self.config.model,
            provider=(
                resolved.provider
                if resolved.provider is not None
                else self.config.provider
            ),
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
        )

    def _executable(self) -> list[str]:
        """The program and fixed arguments, before the mode and run flags.

        A seam for tests: the fake swaps this rather than `_build_command`, so
        the flags under test are still assembled by the production code.
        """
        return ["pi"]

    def _build_command(
        self, settings: _PiRunSettings, session_id: str, *, continuing: bool = False
    ) -> list[str]:
        # Resume by full UUID: a path or --session-id could create a replacement
        # if the looked-up session was deleted, and a partial ID is ambiguous.
        parts: list[str] = [*self._executable(), "--mode", "rpc"]
        parts += ["--session" if continuing else "--session-id", session_id]
        if settings.model is not None:
            parts += ["--model", settings.model]
        if settings.provider is not None:
            parts += ["--provider", settings.provider]
        if settings.thinking is not None:
            parts += ["--thinking", settings.thinking]
        return parts

    async def _probe_backend_version(self) -> tuple[str | None, str | None]:
        try:
            process = await self.command_runner.run_to_completion(
                [*self._executable(), "--version"], timeout=_VERSION_TIMEOUT_SEC
            )
        except TimeoutError:
            return None, "pi version probe timed out"
        except icommandrunner.CommandStartError as error:
            return None, f"pi version probe failed: {error}"
        except Exception as error:  # noqa: BLE001
            return None, f"pi version probe failed: {error}"
        try:
            if process.get_exit_code() != 0:
                return None, "pi version probe reported a non-zero exit"
            output = (process.get_output() or process.get_error_output() or "").strip()
            if not output:
                return None, "pi version probe produced no output"
            return output.splitlines()[0].strip(), None
        except Exception as error:  # noqa: BLE001
            return None, f"pi version probe failed: {error}"

    async def _attach_observation(
        self,
        result: RunAgentTaskRunResult,
        state: PiObservationState,
        requested_session_id: str | None,
        expected: AgentSessionRef | None = None,
    ) -> RunAgentTaskRunResult:
        wanted = expected.session_id if expected is not None else requested_session_id
        if (
            wanted is not None
            and state.session_id is not None
            and not _sessions_equal(state.session_id, wanted)
        ):
            state.gaps.append(
                "pi session id differs from the requested capture session"
            )
        native_root = _native_root_for(state.session_file)
        observation = pi_history.locate_pi_history(state, native_root)
        version, version_gap = await self._probe_backend_version()
        gaps = [*observation.gaps]
        if version_gap is not None:
            gaps.append(version_gap)
        observation = dataclasses.replace(
            observation, backend_version=version, gaps=gaps
        )
        return dataclasses.replace(result, observation=observation)
