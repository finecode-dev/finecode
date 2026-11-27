import asyncio
import dataclasses
import pathlib
import shutil
import tempfile
import time
import typing

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    ilogger,
    iprojectactionrunner,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import (
    path_to_resource_uri,
    resource_uri_to_path,
)

from fine_envs import create_env_action, create_envs_action
from fine_envs.project_config_dump import DUMP_FILE_NAME, write_project_config_dump


@dataclasses.dataclass
class CreateEnvsDispatchHandlerConfig(code_action.ActionHandlerConfig):
    pass


class CreateEnvsDispatchHandler(
    code_action.ActionHandler[
        create_envs_action.CreateEnvsAction, CreateEnvsDispatchHandlerConfig
    ]
):
    """Dispatch a create_env call per environment concurrently."""

    def __init__(
        self,
        config: CreateEnvsDispatchHandlerConfig,
        action_runner: iprojectactionrunner.IProjectActionRunner,
        logger: ilogger.ILogger,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
    ) -> None:
        self.config = config
        self.action_runner = action_runner
        self.logger = logger
        self.project_info_provider = project_info_provider

    def _remove_shared_dump(self, dump_dir: pathlib.Path) -> None:
        try:
            shutil.rmtree(dump_dir)
        except OSError as exc:
            self.logger.warning(
                f"Failed to remove the shared config dump directory {dump_dir}: {exc}"
            )

    async def run(
        self,
        payload: create_envs_action.CreateEnvsRunPayload,
        run_context: create_envs_action.CreateEnvsRunContext,
    ) -> create_envs_action.CreateEnvsRunResult:
        if run_context.envs is None:
            raise code_action.ActionFailedException(
                "envs must be either provided in payload or be discovered by previous `create_envs` handlers"
            )

        ordered_group_paths: list[pathlib.Path] = []
        groups: dict[pathlib.Path, list[create_envs_action.EnvInfo]] = {}
        for env in run_context.envs:
            key = resource_uri_to_path(env.project_def_path)
            if key not in groups:
                groups[key] = []
                ordered_group_paths.append(key)
            groups[key].append(env)

        run_dir: pathlib.Path | None = None
        group_files: dict[pathlib.Path, pathlib.Path] = {}
        if ordered_group_paths:
            run_dir = pathlib.Path(tempfile.mkdtemp(prefix="finecode_config_dump_"))
            used_names: set[str] = set()
            name_counts: dict[str, int] = {}
            for group_path in ordered_group_paths:
                base = group_path.parent.name or "project"
                if base not in used_names:
                    name = base
                else:
                    n = name_counts.get(base, 2)
                    while f"{base}-{n}" in used_names:
                        n += 1
                    name = f"{base}-{n}"
                    name_counts[base] = n + 1
                if base not in name_counts:
                    name_counts[base] = 2
                used_names.add(name)
                subdir = run_dir / name
                subdir.mkdir(parents=True, exist_ok=True)
                group_files[group_path] = subdir / DUMP_FILE_NAME
            start = time.monotonic()
            try:
                async with asyncio.TaskGroup() as pg:
                    for group_path in ordered_group_paths:
                        _ = pg.create_task(
                            self._prepare_one(
                                group_path, group_files[group_path], run_context
                            )
                        )
            except BaseException as exc:
                if run_dir is not None:
                    self._remove_shared_dump(run_dir)
                    run_dir = None
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if isinstance(exc, BaseExceptionGroup):
                    messages: list[str] = []
                    for sub in exc.exceptions:
                        if isinstance(sub, asyncio.CancelledError):
                            continue
                        if isinstance(sub, BaseExceptionGroup):
                            for inner in sub.exceptions:
                                if isinstance(inner, asyncio.CancelledError):
                                    continue
                                messages.append(str(inner))
                        else:
                            messages.append(str(sub))
                    if not messages:
                        raise
                    raise code_action.ActionFailedException(
                        ". ".join(messages)
                    ) from exc
                raise code_action.ActionFailedException(str(exc)) from exc
            else:
                seconds = time.monotonic() - start
                self.logger.debug(
                    f"Prepared project definitions of {len(ordered_group_paths)} project(s) in {seconds:.3f} s"
                )

        tasks: list[asyncio.Task[create_envs_action.CreateEnvsRunResult]] = []
        fan_out_started = False
        try:
            async with run_context.progress(
                "Creating environments", total=len(run_context.envs)
            ) as progress:

                async def _create_and_advance(
                    env: create_envs_action.EnvInfo,
                ) -> create_envs_action.CreateEnvsRunResult:
                    target = group_files.get(resource_uri_to_path(env.project_def_path))
                    child_payload = create_env_action.CreateEnvRunPayload(
                        env=env,
                        recreate=payload.recreate,
                        resolved_project_def_path=(
                            path_to_resource_uri(target) if target is not None else None
                        ),
                    )
                    result = typing.cast(
                        create_envs_action.CreateEnvsRunResult,
                        await self.action_runner.run_action(
                            action_type=iprojectactionrunner.ActionRef.from_type(
                                create_env_action.CreateEnvAction
                            ),
                            payload=child_payload,
                            meta=run_context.meta,
                        ),
                    )
                    label = create_envs_action.env_label(env)
                    verb = "Created" if result.created else "Already exists"
                    await progress.advance(message=f"{verb}: {label}")
                    return result

                try:
                    async with asyncio.TaskGroup() as tg:
                        fan_out_started = True
                        for env in run_context.envs:
                            tasks.append(tg.create_task(_create_and_advance(env)))
                finally:
                    if (
                        fan_out_started
                        and run_dir is not None
                        and run_context.delegated_calls_outcome()
                        is code_action.DelegatedCallsOutcome.UNCONFIRMED
                    ):
                        projects = ", ".join(str(p.parent) for p in ordered_group_paths)
                        self.logger.warning(
                            f"{projects}: a delegated call of this create_envs run may still be "
                            "running, possibly a create_env handler still using the removed config dump"
                        )
                    if run_dir is not None:
                        self._remove_shared_dump(run_dir)
                        run_dir = None
        except ExceptionGroup as eg:
            error_str = ". ".join([str(e) for e in eg.exceptions])
            raise code_action.ActionFailedException(error_str) from eg
        finally:
            if run_dir is not None:
                self._remove_shared_dump(run_dir)
                run_dir = None

        errors: list[str] = []
        for task in tasks:
            errors += task.result().errors
        return create_envs_action.CreateEnvsRunResult(errors=errors)

    async def _prepare_one(
        self,
        group_path: pathlib.Path,
        target_file: pathlib.Path,
        run_context: create_envs_action.CreateEnvsRunContext,
    ) -> None:
        try:
            await write_project_config_dump(
                project_def_path=group_path,
                target_file=target_file,
                action_runner=self.action_runner,
                project_info_provider=self.project_info_provider,
                logger=self.logger,
                meta=run_context.meta,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise code_action.ActionFailedException(
                f"Failed to prepare the config dump of {group_path}: {exc}"
            ) from exc
