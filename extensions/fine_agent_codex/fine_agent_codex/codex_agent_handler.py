import asyncio
import contextlib
import dataclasses
import enum
import json
import math
import os
import pathlib
import tempfile
import time
import typing

from fine_agent import backend_support
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
from finecode_extension_api.resource_uri import resource_uri_to_path

from fine_agent_codex import codex_history, codex_stream
from fine_agent_codex.codex_history import CodexObservationState

_EXIT_GRACE_SEC = 5.0
_SIGNAL_GRACE_SEC = 2.0
_VERSION_TIMEOUT_SEC = 3.0


class CodexSandboxMode(enum.StrEnum):
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    DANGER_FULL_ACCESS = "danger-full-access"


@dataclasses.dataclass
class CodexAgentProfile:
    model: str | None = None
    settle_timeout_sec: float | None = None


@dataclasses.dataclass
class CodexAgentHandlerConfig(code_action.ActionHandlerConfig):
    model: str | None = None
    sandbox_mode: CodexSandboxMode = CodexSandboxMode.READ_ONLY
    skip_git_repo_check: bool = False
    settle_timeout_sec: float = 900.0
    profiles: dict[str, CodexAgentProfile] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class _CodexRunSettings:
    model: str | None
    settle_timeout_sec: float


def _codex_provenance(usage: AgentRunUsage | None) -> UsageProvenance:
    if usage is None:
        return UsageProvenance(
            token_source=UsageSource.UNKNOWN,
            token_scope=UsageScope.UNKNOWN,
            cost_source=UsageSource.UNKNOWN,
            cost_scope=UsageScope.UNKNOWN,
        )
    return UsageProvenance(
        token_source=UsageSource.CODEX_TURN,
        token_scope=UsageScope.UNKNOWN,
        cost_source=UsageSource.UNKNOWN,
        cost_scope=UsageScope.UNKNOWN,
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
        backend=AgentBackend.CODEX,
        backend_version=None,
        session_id=None,
        history_root=None,
        history_status=HistoryDiscoveryStatus.UNAVAILABLE,
        files=[],
        related_status=RelatedHistoryStatus.UNAVAILABLE,
        usage_provenance=_unknown_provenance(),
        gaps=list(gaps),
    )


def _codex_home() -> pathlib.Path:
    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        return pathlib.Path(env_home)
    return pathlib.Path.home() / ".codex"


@dataclasses.dataclass
class _CodexRunState:
    last_assistant_text: str = ""
    usage: AgentRunUsage | None = None
    thread_id: str | None = None
    terminal: (
        typing.Literal[
            codex_stream.CodexEventKind.TURN_COMPLETED,
            codex_stream.CodexEventKind.TURN_FAILED,
        ]
        | None
    ) = None
    error: str | None = None


class CodexAgentHandler(
    code_action.ActionHandler[
        RunAgentTaskAction,
        CodexAgentHandlerConfig,
    ],
):
    def __init__(
        self,
        config: CodexAgentHandlerConfig,
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
            failed = RunAgentTaskRunResult(
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
                failed = dataclasses.replace(failed, observation=observation)
            return failed

        started = time.monotonic()
        tmpdir = tempfile.TemporaryDirectory()
        try:
            answer_path = pathlib.Path(tmpdir.name) / "answer.txt"
            schema_path: pathlib.Path | None = None
            if payload.output_schema is not None:
                schema_path = pathlib.Path(tmpdir.name) / "schema.json"
                try:
                    _ = schema_path.write_text(
                        json.dumps(payload.output_schema), encoding="utf-8"
                    )
                except OSError as error:
                    failed = RunAgentTaskRunResult(
                        status=AgentRunStatus.FAILED,
                        error=f"could not write codex schema file: {error}",
                        duration_sec=time.monotonic() - started,
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
                        failed = dataclasses.replace(failed, observation=observation)
                    return failed
            command = self._build_command(settings, answer_path, schema_path)
            project_dir = self.project_info_provider.get_current_project_dir_path()
            self.logger.debug(f"Starting codex: {command!r} in {project_dir}")

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
                    failed = dataclasses.replace(failed, observation=observation)
                return failed

            state = _CodexRunState()
            result: RunAgentTaskRunResult | None = None
            try:
                result = await self._run_with_process(
                    payload, run_context, process, command, settings, answer_path, state
                )
            finally:
                teardown_error = await self._teardown(process)
                if teardown_error is not None:
                    self.logger.warning(teardown_error)
                    if result is not None and result.status is AgentRunStatus.SETTLED:
                        result = RunAgentTaskRunResult(
                            status=AgentRunStatus.FAILED,
                            output=result.output,
                            usage=result.usage,
                            error=teardown_error,
                        )

            result.duration_sec = time.monotonic() - started
            if payload.capture_history:
                try:
                    result = await self._attach_observation(result, state)
                except Exception as error:  # noqa: BLE001
                    gaps = [
                        f"observation failed: {error}",
                    ]
                    self.logger.debug(f"codex observation failed: {error}")
                    failed_obs = AgentRunObservation(
                        backend=AgentBackend.CODEX,
                        backend_version=None,
                        session_id=state.thread_id,
                        history_root=None,
                        history_status=HistoryDiscoveryStatus.FAILED,
                        files=[],
                        related_status=RelatedHistoryStatus.UNAVAILABLE,
                        usage_provenance=_codex_provenance(state.usage),
                        gaps=gaps,
                    )
                    result = dataclasses.replace(result, observation=failed_obs)
            return result
        finally:
            tmpdir.cleanup()

    async def _run_with_process(
        self,
        payload: RunAgentTaskRunPayload,
        run_context: RunAgentTaskRunContext,
        process: icommandrunner.IAsyncProcess,
        command: list[str],
        settings: _CodexRunSettings,
        answer_path: pathlib.Path,
        state: _CodexRunState,
    ) -> RunAgentTaskRunResult:
        async with run_context.progress("Agent task", cancellable=True) as progress:
            try:
                await asyncio.wait_for(
                    self._drive(process, payload.prompt, progress, state),
                    timeout=settings.settle_timeout_sec,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                return RunAgentTaskRunResult(
                    status=AgentRunStatus.FAILED,
                    output=state.last_assistant_text,
                    usage=state.usage,
                    error=(
                        f"codex did not finish within {settings.settle_timeout_sec}s"
                    ),
                )

        return await self._finish(process, state, answer_path, payload, command)

    async def _drive(
        self,
        process: icommandrunner.IAsyncProcess,
        prompt: str,
        progress: code_action.ProgressContext,
        state: _CodexRunState,
    ) -> None:
        lines = process.stdout_lines()
        process.write_to_stdin(prompt)
        process.close_stdin()

        async for line in lines:
            event = codex_stream.parse_event(line)
            if event is None:
                self.logger.debug("Ignoring non-JSON line from codex")
                continue

            if event.kind is codex_stream.CodexEventKind.THREAD_STARTED:
                if event.thread_id is not None:
                    state.thread_id = event.thread_id
                    self.logger.info(f"codex thread {event.thread_id}")
                continue

            if event.kind is codex_stream.CodexEventKind.TURN_STARTED:
                await progress.report("Agent turn started")
                continue

            if event.kind in (
                codex_stream.CodexEventKind.ITEM_STARTED,
                codex_stream.CodexEventKind.ITEM_UPDATED,
                codex_stream.CodexEventKind.ITEM_COMPLETED,
            ):
                if event.narrative is not None:
                    await progress.report(event.narrative)
                if event.assistant_text is not None:
                    state.last_assistant_text = event.assistant_text
                continue

            if event.kind is codex_stream.CodexEventKind.ERROR:
                if event.error is not None:
                    state.error = event.error
                continue

            if event.kind is codex_stream.CodexEventKind.TURN_COMPLETED:
                state.terminal = codex_stream.CodexEventKind.TURN_COMPLETED
                if event.usage is not None:
                    state.usage = event.usage
                break

            if event.kind is codex_stream.CodexEventKind.TURN_FAILED:
                state.terminal = codex_stream.CodexEventKind.TURN_FAILED
                state.error = event.error or "turn failed"
                if event.usage is not None:
                    state.usage = event.usage
                break

    async def _finish(
        self,
        process: icommandrunner.IAsyncProcess,
        state: _CodexRunState,
        answer_path: pathlib.Path,
        payload: RunAgentTaskRunPayload,
        command: list[str],
    ) -> RunAgentTaskRunResult:
        with contextlib.suppress(TimeoutError):
            await process.wait_for_end(timeout=_EXIT_GRACE_SEC)

        exit_code = process.get_exit_code()
        stderr = process.get_error_output()
        self.logger.debug(f"codex exited with {exit_code}; stderr: {stderr}")

        if process.is_alive() and exit_code is None:
            return RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED,
                output=state.last_assistant_text,
                usage=state.usage,
                error=f"codex did not exit within {_EXIT_GRACE_SEC}s",
            )

        if state.terminal is None:
            reason = "codex stopped without reporting a result"
            if state.error is not None:
                reason = f"{reason}: {state.error}"
            return RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED,
                output=state.last_assistant_text,
                usage=state.usage,
                error=reason,
            )

        if state.terminal is codex_stream.CodexEventKind.TURN_FAILED:
            return RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED,
                output=state.last_assistant_text,
                usage=state.usage,
                error=state.error or "turn failed",
            )

        if exit_code not in (0, None):
            reason = f"codex exited with code {exit_code}: {stderr.strip()}"
            return RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED,
                output=state.last_assistant_text,
                usage=state.usage,
                error=reason,
            )

        try:
            text = await asyncio.to_thread(answer_path.read_text, encoding="utf-8")
        except OSError as error:
            return RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED,
                output=state.last_assistant_text,
                usage=state.usage,
                error=f"could not read codex answer file: {error}",
            )

        if payload.output_schema is not None:
            try:
                value = json.loads(text)
            except ValueError as error:
                return RunAgentTaskRunResult(
                    status=AgentRunStatus.FAILED,
                    output=text,
                    usage=state.usage,
                    error=f"invalid JSON in codex answer: {error}",
                )
            if value is None:
                return RunAgentTaskRunResult(
                    status=AgentRunStatus.FAILED,
                    output=text,
                    usage=state.usage,
                    error="codex returned JSON null",
                )
            return RunAgentTaskRunResult(
                status=AgentRunStatus.SETTLED,
                output=text,
                usage=state.usage,
                structured_output=value,
            )

        if not text:
            self.user_messenger.warning(
                f"Agent settled without output (exit {exit_code}). Command: {command!r}"
            )
        return RunAgentTaskRunResult(
            status=AgentRunStatus.SETTLED,
            output=text,
            usage=state.usage,
        )

    async def _teardown(self, process: icommandrunner.IAsyncProcess) -> str | None:
        with contextlib.suppress(Exception):
            process.close_stdin()

        if not process.is_alive():
            return None

        # The leader's drain is already over by the time this runs -- what is
        # being waited for is the group, so poll group liveness rather than
        # the leader's exit.
        with contextlib.suppress(Exception):
            process.terminate()
        if await self._await_group_exit(process, _SIGNAL_GRACE_SEC):
            return None

        with contextlib.suppress(Exception):
            process.kill()
        if await self._await_group_exit(process, _SIGNAL_GRACE_SEC):
            return None
        return "codex did not stop; a child process may still be running"

    async def _await_group_exit(
        self, process: icommandrunner.IAsyncProcess, timeout: float
    ) -> bool:
        deadline = time.monotonic() + timeout
        while process.is_alive():
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.05)
        return True

    def _resolve(self, profile: str | None) -> _CodexRunSettings | str:
        if profile is None:
            model = self.config.model
            timeout = self.config.settle_timeout_sec
        else:
            resolved = self.config.profiles.get(profile)
            if resolved is None:
                return backend_support.unknown_profile_error(
                    profile, self.config.profiles
                )
            model = resolved.model if resolved.model is not None else self.config.model
            timeout = (
                resolved.settle_timeout_sec
                if resolved.settle_timeout_sec is not None
                else self.config.settle_timeout_sec
            )
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(float(timeout))
            or float(timeout) <= 0
        ):
            return (
                f"invalid settle_timeout_sec {timeout!r}: "
                "must be a positive finite number"
            )
        return _CodexRunSettings(model=model, settle_timeout_sec=float(timeout))

    def _executable(self) -> list[str]:
        return ["codex"]

    def _build_command(
        self,
        settings: _CodexRunSettings,
        answer_path: pathlib.Path,
        schema_path: pathlib.Path | None,
    ) -> list[str]:
        parts = [
            *self._executable(),
            "--ask-for-approval",
            "never",
            "exec",
            "--json",
            "--color",
            "never",
            "--sandbox",
            self.config.sandbox_mode.value,
            "--output-last-message",
            str(answer_path),
        ]
        if settings.model is not None:
            parts += ["--model", settings.model]
        if self.config.skip_git_repo_check:
            parts += ["--skip-git-repo-check"]
        if schema_path is not None:
            parts += ["--output-schema", str(schema_path)]
        parts += ["-"]
        return parts

    async def _probe_backend_version(self) -> tuple[str | None, str | None]:
        try:
            process = await self.command_runner.run([*self._executable(), "--version"])
        except (OSError, icommandrunner.CommandNotLaunchableError) as error:
            return None, f"codex version probe failed: {error}"
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
            return None, "codex version probe timed out"
        except Exception as error:  # noqa: BLE001
            return None, f"codex version probe failed: {error}"
        try:
            if process.get_exit_code() != 0:
                return None, "codex version probe reported a non-zero exit"
            output = (process.get_output() or process.get_error_output() or "").strip()
            if not output:
                return None, "codex version probe produced no output"
            return output.splitlines()[0].strip(), None
        except Exception as error:  # noqa: BLE001
            return None, f"codex version probe failed: {error}"

    async def _attach_observation(
        self,
        result: RunAgentTaskRunResult,
        state: _CodexRunState,
    ) -> RunAgentTaskRunResult:
        obs_state = CodexObservationState(
            thread_id=state.thread_id,
            usage_provenance=_codex_provenance(state.usage),
            gaps=[],
        )
        observation = codex_history.locate_codex_history(obs_state, _codex_home())
        if observation.backend_version is None:
            version, version_gap = await self._probe_backend_version()
            gaps = [*observation.gaps]
            if version_gap is not None:
                gaps.append(version_gap)
            observation = dataclasses.replace(
                observation, backend_version=version, gaps=gaps
            )
        result = dataclasses.replace(result, observation=observation)
        main = next(
            (f for f in observation.files if f.role is NativeHistoryRole.MAIN), None
        )
        try:
            main_path = resource_uri_to_path(main.source_uri) if main else None
        except ValueError:
            main_path = None
        if main_path is None:
            return result
        facts = await asyncio.to_thread(codex_history.read_rollout_facts, main_path)
        # The `--json` stream names neither the model nor the turns; the
        # rollout does, so a captured run reports them from there.
        usage = result.usage
        if usage is not None:
            usage = dataclasses.replace(
                usage,
                provider=usage.provider if usage.provider is not None else facts.provider,
                model=usage.model if usage.model is not None else facts.model,
            )
        return dataclasses.replace(
            result,
            usage=usage,
            turns=result.turns if result.turns is not None else facts.turns,
        )
