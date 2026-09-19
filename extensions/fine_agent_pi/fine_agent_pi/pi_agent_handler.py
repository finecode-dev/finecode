import asyncio
import contextlib
import dataclasses
import shlex
import time
from collections.abc import AsyncIterator
from typing import Any

from fine_agent import backend_support
from fine_agent.run_agent_task_action import (
    AgentRunStatus,
    AgentRunUsage,
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

from fine_agent_pi import pi_rpc

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

_POLICY_ABORT = "abort"
_POLICY_CANCEL = "cancel"


@dataclasses.dataclass
class PiAgentProfile:
    """Per-role overrides for one named agent run. A `None` field inherits the
    handler's top-level value."""

    model: str | None = None
    provider: str | None = None
    settle_timeout_sec: float | None = None


@dataclasses.dataclass
class PiAgentHandlerConfig(code_action.ActionHandlerConfig):
    model: str | None = None
    """Model pattern passed to `pi --model`. `None` leaves pi's own default.

    Lives here rather than on the payload so the same task definition runs
    against a different model without being edited (PRD-0005 R3).
    """
    provider: str | None = None
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
    this a wedged run holds an ER subprocess slot indefinitely."""
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
    settle_timeout_sec: float


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
        settings = self._resolve(payload.profile)
        if isinstance(settings, str):
            # Fail before spawning anything: an unknown profile is a
            # configuration error, not a run that got partway.
            return RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED, error=settings, duration_sec=0.0
            )
        command = self._build_command(settings)
        project_dir = self.project_info_provider.get_current_project_dir_path()
        self.logger.debug(f"Starting pi: {command} in {project_dir}")

        started = time.monotonic()
        process = await self.command_runner.run(
            command,
            cwd=project_dir,
            # pi runs tools of its own, so tearing it down has to reach them
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
            # Without this, pi is left with stdin open, blocks on it forever,
            # never exits, and its subprocess slot is lost for the ER's
            # lifetime. The teardown is the same one every other exit path uses,
            # and `_abort` is idempotent, so running it again after one of those
            # costs nothing.
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
        settings: _PiRunSettings,
    ) -> RunAgentTaskRunResult:
        prompt = payload.prompt
        if payload.output_schema is not None:
            # Appended, not substituted: the task wording is the caller's, and
            # the output instruction is the only thing the backend adds.
            prompt += backend_support.json_output_instruction(payload.output_schema)

        async with run_context.progress("Agent task", cancellable=True) as progress:
            try:
                result = await asyncio.wait_for(
                    self._drive(process, prompt, progress),
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
                return RunAgentTaskRunResult(
                    status=AgentRunStatus.FAILED,
                    error=(
                        f"pi did not settle within {settings.settle_timeout_sec}s"
                    ),
                )

        return self._extract_structured_output(
            payload, await self._finish(process, result, command)
        )

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
    ) -> RunAgentTaskRunResult:
        # Subscribe before prompting. The stream replays anything produced
        # earlier, so this is not load-bearing -- it just keeps the order on the
        # page the same as the order on the wire.
        lines = process.stdout_lines()
        process.write_to_stdin(pi_rpc.prompt_command(prompt))

        output: list[str] = []
        turns = 0
        usage: AgentRunUsage | None = None
        """Running total from the message frames.

        `None` until pi reports something, so a run that reported nothing stays
        distinguishable from one that reported zeros. Superseded by the session
        totals when the run settles and pi answers -- this accumulation exists
        for the paths where it never gets that far.
        """
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
                usage = await self._read_session_stats(process, lines, usage)
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
                if not frame.get("success", True):
                    self.logger.warning(f"pi rejected a command: {frame}")
                continue

            if event == pi_rpc.PiEvent.EXTENSION_UI_REQUEST:
                refused = await self._answer_ui_request(process, frame)
                if refused is not None:
                    await self._abort(process)
                    break

        if refused is not None:
            return RunAgentTaskRunResult(
                status=AgentRunStatus.REFUSED_INTERACTION,
                output="".join(output),
                turns=turns,
                usage=usage,
                error=refused,
            )

        if not settled:
            # The stream reached EOF without `agent_settled`: pi died, or was
            # killed, mid-run. Whatever text arrived first is kept -- it is
            # still the best account of what happened -- but reporting SETTLED
            # here would tell the caller a truncated run finished its work.
            reason = "pi stopped without settling"
            return RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED,
                output="".join(output),
                turns=turns,
                usage=usage,
                error=f"{failure}; {reason}" if failure else reason,
            )

        if failure is not None:
            # Settled, but the last turn never reached the model. pi reports
            # this only in the message frames and still exits 0, so trusting
            # `agent_settled` alone reports a provider outage -- an expired
            # key, an unfunded account, a rate limit -- as a successful run
            # that happened to say nothing.
            return RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED,
                output="".join(output),
                turns=turns,
                usage=usage,
                error=failure,
            )

        return RunAgentTaskRunResult(
            status=AgentRunStatus.SETTLED,
            output="".join(output),
            turns=turns,
            usage=usage,
        )

    async def _read_session_stats(
        self,
        process: icommandrunner.IAsyncProcess,
        lines: AsyncIterator[str],
        accumulated: AgentRunUsage | None,
    ) -> AgentRunUsage | None:
        """Ask pi for the session totals, falling back to `accumulated`.

        pi's totals are the better number: they also cover what the agent's own
        tools and its context compaction spent, neither of which ever reaches
        the message frames this handler sums. But they are only obtainable
        while pi is still alive and answering, so the summed figure remains the
        answer on every path that does not reach a settle.

        Best-effort throughout. This runs after the run has already produced
        its output, so no failure here may change what is returned.
        """
        with contextlib.suppress(Exception):
            process.write_to_stdin(pi_rpc.session_stats_command())

        stats: AgentRunUsage | None = None
        with contextlib.suppress(TimeoutError):
            stats = await asyncio.wait_for(
                self._await_session_stats(lines), timeout=_STATS_TIMEOUT_SEC
            )

        if stats is None:
            self.logger.debug("pi reported no session stats; using message totals")
            return accumulated

        # The stats response names no provider or model, so the identity comes
        # from the messages. Reattached rather than dropped because a cost with
        # nothing to attribute it to cannot be compared against anything.
        return dataclasses.replace(
            stats,
            provider=accumulated.provider if accumulated else None,
            model=accumulated.model if accumulated else None,
        )

    async def _await_session_stats(
        self, lines: AsyncIterator[str]
    ) -> AgentRunUsage | None:
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
                return pi_rpc.parse_session_stats(frame)
        return None

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
        command: str,
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
                f"Agent settled without output (exit {exit_code}). Command: {command}"
            )

        return result

    async def _abort(self, process: icommandrunner.IAsyncProcess) -> None:
        """Stop pi, escalating until it is actually gone.

        Asking first is worth the grace period: pi runs tools of its own, and
        the polite exit is the one that lets it stop them. But asking is not
        enough on its own -- a pi that ignores `abort` keeps write access to the
        project, keeps spending, and keeps holding the subprocess slot the
        timeout exists to release, so the ladder ends somewhere unconditional.

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
                settle_timeout_sec=self.config.settle_timeout_sec,
            )
        resolved = self.config.profiles.get(profile)
        if resolved is None:
            return backend_support.unknown_profile_error(
                profile, self.config.profiles
            )
        return _PiRunSettings(
            model=resolved.model if resolved.model is not None else self.config.model,
            provider=(
                resolved.provider
                if resolved.provider is not None
                else self.config.provider
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

    def _build_command(self, settings: _PiRunSettings) -> str:
        parts = [*self._executable(), "--mode", "rpc", "--no-session"]
        if settings.model is not None:
            parts += ["--model", settings.model]
        if settings.provider is not None:
            parts += ["--provider", settings.provider]
        return shlex.join(parts)
