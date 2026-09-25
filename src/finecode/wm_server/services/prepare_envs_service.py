"""Server-side orchestration for prepare-envs.

Public API
----------
- ``prepare_envs``: full workspace environment preparation (equivalent to the
  ``prepare-envs`` CLI command), runs server-side without requiring a client.
- ``install_env_for_project``: targeted repair — installs a single named env
  for a project via its dev_workspace runner.

Both functions raise :class:`PrepareEnvsFailed` on failure.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import uuid
from typing import TYPE_CHECKING, Any

from finecode_extension_api.resource_uri import resource_uri_to_path
from loguru import logger

from finecode import user_messages
from finecode.wm_server import context, domain

if TYPE_CHECKING:
    from finecode.wm_server.config.env_selection import EnvSelection


class PrepareEnvsFailed(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def build_create_envs_params(
    sel: EnvSelection, env_universe: dict[str, Any], recreate: bool
) -> dict[str, Any]:
    """Build the `fine_envs.CreateEnvsAction` params for one project's step-5 call.

    `recreate` must always be forwarded here — it's the only place a `--recreate`
    CLI run reaches the per-project `create_envs` step (step 2 forwards it
    separately, but only for the `dev_workspace` env).

    `dev_workspace` is always excluded from this step's env set: it is already
    created for every project by steps 2-3's dedicated bootstrap, which executes
    on the *root* project's runner. By step 5 each project's own `dev_workspace`
    runner is already started, so re-including it here would make that runner
    recreate the very venv it is executing from.
    """
    from finecode.wm_server.config import env_selection

    if sel.active:
        create_set = env_selection.compute_create_set(sel, set(env_universe.keys()))
        create_set.discard("dev_workspace")
        return {"recreate": recreate, "env_names": sorted(create_set)}
    elif "dev_workspace" in env_universe:
        create_set = set(env_universe.keys())
        create_set.discard("dev_workspace")
        return {"recreate": recreate, "env_names": sorted(create_set)}
    return {"recreate": recreate}


def project_fan_out_budget(work_cap: int, project_count: int) -> domain.RunBudget:
    """Each member of a prepare-envs project fan-out waits for its share of the budget.

    ``work_cap // project_count`` (never below one) keeps about ``work_cap``
    projects in flight while holding the fan-out's total at the budget; with
    fewer projects than slots, each still gets the full ``work_cap``.
    """
    if project_count <= 0:
        return domain.RunBudget(waits=True, max_slots=1)
    return domain.RunBudget(waits=True, max_slots=max(1, work_cap // project_count))


async def _run_env_action(
    action_source: str,
    params: dict,
    executor_project: domain.CollectedProject,
    ws_context: context.WorkspaceContext,
    *,
    budget: domain.RunBudget,
) -> str | None:
    """Run a ``fine_envs`` action on *executor_project*'s dev_workspace runner.

    Action reports per-env progress as it
    works through a potentially large ``envs`` batch in one action call. This
    subscribes to that progress stream and
    forwards each ``report`` event as a user message, so a single slow batched
    call still shows which env is currently being created/installed.

    ``budget`` declares how the process budget treats this dispatch (ADR-0094).
    It has no default: a waiting budget and a non-waiting one have opposite
    failure modes, so which one this is must be written at every call site.

    Returns an error string on failure, ``None`` on success.
    """
    from finecode.wm_server.runner import runner_client as rc
    from finecode.wm_server.services import run_service
    from finecode.wm_server.services.run_service import proxy_utils

    action = next(
        (a for a in executor_project.actions if a.source == action_source), None
    )
    if action is None or action.canonical_source is None:
        return f"{action_source} not available in project '{executor_project.name}'"

    progress_token = str(uuid.uuid4())
    progress_list: proxy_utils.AsyncList[domain.ProgressRawValue] = (
        proxy_utils.AsyncList()
    )
    progress_tasks: list[asyncio.Task] = []
    runners_by_env = ws_context.ws_projects_extension_runners.get(
        executor_project.dir_path, {}
    )
    # action may declare multiple handlers in the
    # same env; subscribing once per handler would register two
    # listeners on the same runner+token and double-emit every progress event, so
    # dedupe by env name (== by runner) first.
    subscribed_envs: set[str] = set()
    for handler in action.handlers:
        if handler.env in subscribed_envs:
            continue
        subscribed_envs.add(handler.env)
        runner = runners_by_env.get(handler.env)
        if runner is not None:
            progress_tasks.append(
                asyncio.create_task(
                    proxy_utils.get_progress(
                        result_list=progress_list,
                        progress_token=progress_token,
                        runner=runner,
                    )
                )
            )

    async def _forward_progress() -> None:
        async for value in progress_list:
            if value.get("type") != "report":
                continue
            message = value.get("message")
            if message:
                await user_messages.info(message)

    forward_task = asyncio.create_task(_forward_progress())

    try:
        result = await run_service.ProjectExecutor(ws_context).run_action(
            action_source=action.canonical_source,
            params=params,
            project_path=executor_project.dir_path,
            run_trigger=rc.RunActionTrigger.USER,
            dev_env=rc.DevEnv.CLI,
            result_formats=[rc.RunResultFormat.STRING],
            initialize_all_handlers=True,
            progress_token=progress_token,
            budget=budget,
            # Started by the WM on its own behalf during env preparation, with
            # no client connection behind it.
            origin=None,
        )
    except run_service.ActionRunFailed as action_exc:
        return action_exc.message
    finally:
        for t in progress_tasks:
            t.cancel()
        progress_list.end()
        await asyncio.gather(*progress_tasks, return_exceptions=True)
        forward_task.cancel()
        await asyncio.gather(forward_task, return_exceptions=True)

    if result.return_code != 0:
        return (result.result_by_format or {}).get(
            "string", ""
        ) or f"{action_source} failed"

    return None


_BUILD_PYTHON_ARTIFACT_ACTION = "fine_python_lang.BuildPythonArtifactAction"


def _wheelhouse_dir(workdir_path: pathlib.Path) -> pathlib.Path:
    """Directory holding the built wheels and their manifest.

    It lives under the workspace root's ``dev_workspace`` venv cache (beside the
    WM's discovery file) rather than a separate workspace-root directory: the
    wheelhouse is derived build state, not committed configuration, and a
    recreated ``dev_workspace`` correctly invalidates it.
    """
    return workdir_path / ".venvs" / "dev_workspace" / "cache" / "wheelhouse"


def _write_wheelhouse_manifest(
    workdir_path: pathlib.Path,
    ws_context: context.WorkspaceContext,
    wheels: dict[str, pathlib.Path],
) -> pathlib.Path:
    """Write the wheelhouse manifest atomically and return its directory.

    The manifest is ``{name: {dir, wheel}}``. It is written to a temp file and
    renamed so a reader never observes a half-written file (R4).
    """
    wheelhouse_dir = _wheelhouse_dir(workdir_path)
    wheelhouse_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        name: {
            "dir": ws_context.ws_workspace_packages[name].as_posix(),
            "wheel": wheel.as_posix(),
        }
        for name, wheel in wheels.items()
    }
    manifest_path = wheelhouse_dir / "manifest.json"
    tmp_path = manifest_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(manifest, indent=2))
    tmp_path.replace(manifest_path)
    return wheelhouse_dir


async def _build_wheelhouse(
    ws_context: context.WorkspaceContext,
    workdir_path: pathlib.Path,
    excluded_packages: set[str],
    budget: domain.RunBudget,
) -> dict[str, pathlib.Path]:
    """Build a wheel for every workspace package.

    Each wheel is built by its own package's project runner, so that project's
    handler selection and config apply — never by a single root-env builder.
    A workspace package that is not itself a FineCode project (a preset-only
    library) is built by the workspace root's runner, since it has no builder of
    its own and no per-project config to override.

    The wheelhouse is workspace-wide: any wheel-mode env must resolve *every*
    workspace package it depends on to a wheel, so ``--project`` is rejected in
    wheel mode rather than producing a partial wheelhouse (P5/R4).

    Raises:
        PrepareEnvsFailed: a package's project is not a collected project, does
            not register ``build_python_artifact``, or a build failed.
    """
    from finecode.wm_server.runner import runner_client as rc
    from finecode.wm_server.services import run_service

    packages = {
        name: package_dir
        for name, package_dir in ws_context.ws_workspace_packages.items()
        if name not in excluded_packages
    }

    root_project = ws_context.ws_projects.get(workdir_path)

    plans: list[tuple[str, pathlib.Path, domain.CollectedProject, str]] = []
    for name, package_dir in packages.items():
        builder = ws_context.ws_projects.get(package_dir)
        if not isinstance(builder, domain.CollectedProject):
            # A workspace package that is not itself a FineCode project (e.g. a
            # preset-only library) has no builder of its own. Build it on the
            # workspace root's runner: it has the build handler, and with no
            # per-project FineCode config there is no builder override to
            # respect. A FineCode project still builds on its own runner.
            builder = root_project
        if not isinstance(builder, domain.CollectedProject):
            raise PrepareEnvsFailed(
                f"Workspace package '{name}' at {package_dir} has no builder: neither "
                "it nor the workspace root is a collected project. Add it to "
                "[workspace.workspace_packages_install].exclude to keep it editable."
            )
        action = next(
            (
                candidate
                for candidate in builder.actions
                if candidate.source == _BUILD_PYTHON_ARTIFACT_ACTION
            ),
            None,
        )
        if action is None or action.canonical_source is None:
            raise PrepareEnvsFailed(
                f"Workspace package '{name}': builder project '{builder.name}' does not "
                f"register {_BUILD_PYTHON_ARTIFACT_ACTION}; cannot build its wheel. Add it "
                "to [workspace.workspace_packages_install].exclude to keep it editable."
            )
        plans.append((name, package_dir, builder, action.canonical_source))

    wheelhouse_dir = _wheelhouse_dir(workdir_path)
    wheelhouse_dir.mkdir(parents=True, exist_ok=True)

    wheels: dict[str, pathlib.Path] = {}
    errors: list[str] = []

    async def _build_one(
        name: str,
        package_dir: pathlib.Path,
        project: domain.CollectedProject,
        canonical_source: str,
    ) -> None:
        params = {
            "src_artifact_def_path": (package_dir / "pyproject.toml").as_uri(),
            "distributions": ["wheel"],
            "output_dir": wheelhouse_dir.as_uri(),
        }
        try:
            response = await run_service.ProjectExecutor(ws_context).run_action(
                action_source=canonical_source,
                params=params,
                project_path=project.dir_path,
                run_trigger=rc.RunActionTrigger.USER,
                dev_env=rc.DevEnv.CLI,
                result_formats=[rc.RunResultFormat.JSON],
                initialize_all_handlers=True,
                budget=budget,
                origin=None,
            )
        except run_service.ActionRunFailed as action_exc:
            errors.append(f"{name}: {action_exc.message}")
            return
        if response.return_code != 0:
            errors.append(f"{name}: build failed")
            return
        json_result = response.result_by_format.get("json") or {}
        output_paths = json_result.get("build_output_paths") or []
        wheel_path = next(
            (
                resource_uri_to_path(uri)
                for uri in output_paths
                if str(uri).endswith(".whl")
            ),
            None,
        )
        if wheel_path is None:
            errors.append(f"{name}: build reported no wheel")
            return
        wheels[name] = wheel_path

    await asyncio.gather(*(_build_one(name, d, p, src) for name, d, p, src in plans))
    if errors:
        raise PrepareEnvsFailed(
            "'build_python_artifact' failed for workspace packages:\n"
            + "\n".join(sorted(errors))
        )
    return wheels


async def prepare_envs(
    ws_context: context.WorkspaceContext,
    workdir_path: pathlib.Path,
    recreate: bool = False,
    env_names: list[str] | None = None,
    interpreter_names: list[str] | None = None,
    project_names: list[str] | None = None,
    dev_env: str = "cli",
    workspace_packages_mode: str | None = None,
) -> None:
    """Prepare all virtual environments for a workspace.

    Server-side equivalent of the ``prepare-envs`` CLI command. Orchestrates:
    1. Project discovery.
    2. Check / remove dev_workspace environments.
    2.5. Start workspace root dev_workspace runner.
    3. create_envs + install_envs for subproject dev_workspace envs.
    4. Start all dev_workspace runners.
    4.5. Install each project's dev_workspace env (preset-resolved deps).
    4.6. Wheel mode only: build a wheel for every workspace package, each by
         its own project's runner, into
         ``<ws_root>/.venvs/dev_workspace/cache/wheelhouse``.
    5. create_envs across all projects (skips unselected matrix children).
    6. install_envs across all projects (installs the selected non-dev_workspace
       envs; in wheel mode they install from the wheelhouse).

    Args:
        ws_context: Workspace context.
        workdir_path: Absolute path to the workspace root directory.
        recreate: When True, delete and recreate all dev_workspace venvs.
        env_names: Limit to these env names. For a matrix env, naming its
            base selects all of its children; naming a concrete child selects
            only that child.
        interpreter_names: Limit matrix envs to these interpreters (canonical
            or version-only shorthand), across every matrix base.
        project_names: Limit steps 3, 5, and 6 to these projects.
        dev_env: Active dev-env, used to resolve each matrix env's
            config-declared ``default_interpreters`` subset when
            `interpreter_names` is not given.
    Per project, `env_names` / `interpreter_names` / each matrix env's
    `default_interpreters` policy are resolved (via
    `finecode.wm_server.config.env_selection`) into a selection. When that
    selection is active (a proper subset of the project's envs): step 5
    skips unselected matrix children (non-matrix envs are still created —
    PRD-0003 AC8), and step 6 installs only the selected envs. When inactive
    (no selectors and no narrowing config default anywhere), both steps cover
    every env — today's behaviour (R7).

    Raises:
        PrepareEnvsFailed: if any step fails, or if an `--env` /
            `--interpreter` selector matches no env in any in-scope project,
            or if a config default / selector names an interpreter not in its
            matrix env's declared axis.
    """
    from finecode.wm_server.config import env_selection, read_configs
    from finecode.wm_server.runner import runner_manager
    from finecode.wm_server.services import runner_start_service
    from finecode.wm_server.services.run_service.run_selection import (
        project_env_universe_from_raw,
    )

    # Step 1 — Discover projects.
    logger.info("Discovering projects...")
    await user_messages.info("Discovering projects...")
    if workdir_path not in ws_context.ws_dirs_paths:
        ws_context.ws_dirs_paths.append(workdir_path)
        await read_configs.read_projects_in_dir(workdir_path, ws_context)

    for project in list(ws_context.ws_projects.values()):
        if (
            project.dir_path.is_relative_to(workdir_path)
            and project.dir_path not in ws_context.ws_projects_raw_configs
        ):
            read_configs.read_project_config(project=project, ws_context=ws_context)

    ws_context.ws_workspace_packages = read_configs.resolve_workspace_packages(
        ws_context
    )
    ws_context.workspace_packages_install_mode = (
        workspace_packages_mode
        or read_configs.resolve_workspace_packages_install_mode(ws_context, dev_env)
    )
    ws_context.ws_workspace_packages_install_exclude = set(
        read_configs.resolve_workspace_packages_install_exclude(ws_context)
    )
    resolved_install_mode = ws_context.workspace_packages_install_mode
    if resolved_install_mode == "wheel" and project_names is not None:
        raise PrepareEnvsFailed(
            "prepare-envs --workspace-packages=wheel builds a workspace-wide "
            "wheelhouse and cannot be combined with --project; run it without "
            "--project (or use --workspace-packages=editable for a filtered run)."
        )
    # The dev_workspace envs are the builders: they must install editable before
    # the wheelhouse exists (steps 3 and 4.5). The resolved mode is applied just
    # before the non-dev_workspace envs are installed (step 4.6 onward).
    ws_context.workspace_packages_install_mode = "editable"
    logger.info(f"Workspace packages install mode: {resolved_install_mode}")

    workdir_project = ws_context.ws_projects.get(workdir_path)
    if workdir_project is None:
        raise PrepareEnvsFailed(
            "prepare-envs can be run only from workspace/project root"
        )

    projects = [
        p
        for p in ws_context.ws_projects.values()
        if p.dir_path.is_relative_to(workdir_path)
    ]

    invalid_projects = [
        p for p in projects if p.status == domain.ProjectStatus.CONFIG_INVALID
    ]
    if invalid_projects:
        names = [p.name for p in invalid_projects]
        raise PrepareEnvsFailed(f"Projects have invalid configuration: {names}")

    other_projects = [
        p
        for p in projects
        if p.dir_path != workdir_path and p.status == domain.ProjectStatus.CONFIG_VALID
    ]

    project_paths_filter: list[str] | None = None
    if project_names is not None:
        unknown = [n for n in project_names if not any(p.name == n for p in projects)]
        if unknown:
            raise PrepareEnvsFailed(f"Unknown project(s): {unknown}")
        other_projects = [p for p in other_projects if p.name in project_names]
        project_paths_filter = [
            str(p.dir_path) for p in projects if p.name in project_names
        ]

    logger.info(f"Found {len(projects)} project(s): {[p.name for p in projects]}")
    await user_messages.info(f"Found {len(projects)} project(s)")

    # Step 2 — Check / remove dev_workspace envs.
    logger.info("Checking dev workspace environments...")
    await user_messages.info("Checking dev workspace environments...")

    async def _check_or_remove(project: domain.Project) -> None:
        if recreate:
            logger.trace(f"Recreating dev_workspace for '{project.name}'")
            runners = ws_context.ws_projects_extension_runners.get(project.dir_path, {})
            runner = runners.get("dev_workspace")
            if runner is not None:
                await runner_manager.stop_extension_runner(
                    runner=runner, ws_context=ws_context
                )
            await runner_manager.remove_runner_env(project.dir_path, "dev_workspace")
        else:
            check = await runner_manager.check_runner_within_budget(
                ws_context, runner_dir=project.dir_path, env_name="dev_workspace"
            )
            if not check.valid:
                logger.warning(
                    f"Env 'dev_workspace' in project '{project.name}' is invalid"
                    f" ({check.reason}), recreating it"
                )
                runners = ws_context.ws_projects_extension_runners.get(
                    project.dir_path, {}
                )
                runner = runners.get("dev_workspace")
                if runner is not None:
                    await runner_manager.stop_extension_runner(
                        runner=runner, ws_context=ws_context
                    )
                await runner_manager.remove_runner_env(
                    project.dir_path, "dev_workspace"
                )

    try:
        async with asyncio.TaskGroup() as tg:
            for project in other_projects:
                tg.create_task(_check_or_remove(project))
    except* PrepareEnvsFailed as eg:
        raise eg.exceptions[0]
    except* Exception as eg:
        raise PrepareEnvsFailed(
            f"Failed to check/remove environments: {eg.exceptions[0]}"
        ) from eg.exceptions[0]

    # Step 2.5 — Start workspace root dev_workspace runner.
    if other_projects:
        logger.info("Starting workspace root dev_workspace runner...")
        await user_messages.info("Starting workspace root dev_workspace runner...")
        try:
            await runner_start_service.start_runners_with_auto_prepare(
                projects=[workdir_project],
                ws_context=ws_context,
                initialize_all_handlers=True,
            )
        except Exception as exc:
            raise PrepareEnvsFailed(
                f"Starting workspace root runner failed: {exc}"
            ) from exc

    # Step 3 — create_envs + install_envs for subproject dev_workspace envs.
    logger.info("Creating/updating dev workspace environments...")
    await user_messages.info("Creating/updating dev workspace environments...")
    root_project = ws_context.ws_projects.get(workdir_path)
    dw_envs = [
        {
            "name": "dev_workspace",
            "venv_dir_path": (p.dir_path / ".venvs" / "dev_workspace").as_uri(),
            "project_def_path": (p.dir_path / "pyproject.toml").as_uri(),
        }
        for p in other_projects
    ]
    if dw_envs and isinstance(root_project, domain.CollectedProject):
        error = await _run_env_action(
            "fine_envs.CreateEnvsAction",
            {"envs": dw_envs},
            root_project,
            ws_context,
            # One batched run on the root runner, not a fan-out: it needs its
            # full grant to create every subproject's dev_workspace venv.
            budget=domain.RunBudget(),
        )
        if error:
            raise PrepareEnvsFailed(f"dev_workspace create_envs failed: {error}")

        error = await _run_env_action(
            "fine_envs.InstallEnvsAction",
            {"envs": dw_envs},
            root_project,
            ws_context,
            # Same batched bootstrap run as above; full grant, no fan-out.
            budget=domain.RunBudget(),
        )
        if error:
            raise PrepareEnvsFailed(f"dev_workspace install_envs failed: {error}")
    elif not dw_envs:
        logger.info("No dev_workspace environments to bootstrap, skipping")

    # Step 4 — Start all dev_workspace runners.
    logger.info("Starting dev_workspace runners...")
    await user_messages.info("Starting dev_workspace runners...")
    projects_to_start: list[domain.Project]
    if project_paths_filter is not None:
        projects_to_start = [
            p
            for p in ws_context.ws_projects.values()
            if str(p.dir_path) in project_paths_filter
        ]
    else:
        projects_to_start = [
            p
            for p in ws_context.ws_projects.values()
            if p.dir_path.is_relative_to(workdir_path)
            and p.status == domain.ProjectStatus.CONFIG_VALID
        ]
    try:
        await runner_start_service.start_runners_with_auto_prepare(
            projects=projects_to_start,
            ws_context=ws_context,
        )
    except Exception as exc:
        raise PrepareEnvsFailed(f"Starting runners failed: {exc}") from exc

    # Step 5 — create_envs across all projects.
    logger.info("Creating envs...")
    step_projects = [
        p
        for p in ws_context.ws_projects.values()
        if isinstance(p, domain.CollectedProject)
        and p.dir_path.is_relative_to(workdir_path)
        and (project_paths_filter is None or str(p.dir_path) in project_paths_filter)
    ]
    total_projects = len(step_projects)
    fan_out_budget = project_fan_out_budget(
        ws_context.process_budget.size, total_projects
    )

    # Step 4.5 — preset-resolved install of each project's dev_workspace env.
    # Split out of step 6 because the wheelhouse build (4.6) needs each
    # project's dev_workspace to carry its preset-resolved handlers (the build
    # handlers among them) before it can run. The dev_workspace envs are the
    # builders and the wheelhouse cannot exist before them, so they install
    # editables even in wheel mode — the wheel map is still empty here.
    logger.info("Installing dev_workspace environments...")
    await user_messages.info("Installing dev_workspace environments...")
    dev_workspace_install_errors: list[str] = []

    async def _install_dev_workspace_one(p: domain.CollectedProject) -> None:
        err = await _run_env_action(
            "fine_envs.InstallEnvsAction",
            {"env_names": ["dev_workspace"]},
            p,
            ws_context,
            budget=fan_out_budget,
        )
        if err:
            dev_workspace_install_errors.append(err)

    await asyncio.gather(*(_install_dev_workspace_one(p) for p in step_projects))
    if dev_workspace_install_errors:
        raise PrepareEnvsFailed(
            "'install_envs' failed for dev_workspace:\n"
            + "\n".join(dev_workspace_install_errors)
        )

    # Step 4.6 — build the wheelhouse (wheel mode only).
    if resolved_install_mode == "wheel":
        ws_context.workspace_packages_install_mode = "wheel"
        logger.info("Building workspace package wheels...")
        await user_messages.info("Building workspace package wheels...")
        excluded_packages = ws_context.ws_workspace_packages_install_exclude
        wheels = await _build_wheelhouse(
            ws_context=ws_context,
            workdir_path=workdir_path,
            excluded_packages=excluded_packages,
            budget=fan_out_budget,
        )
        ws_context.ws_workspace_package_wheels = wheels
        wheelhouse_dir = _write_wheelhouse_manifest(workdir_path, ws_context, wheels)
        logger.info(
            f"Built {len(wheels)} workspace package wheel(s) into {wheelhouse_dir}"
        )
    else:
        ws_context.workspace_packages_install_mode = "editable"
        ws_context.ws_workspace_package_wheels = {}

    def _project_env_universe(p: domain.CollectedProject) -> dict[str, Any]:
        """The project's full env-name -> `tool.finecode.env` entry map.

        Delegates to the shared `run_selection.project_env_universe_from_raw`
        helper (also used by run entry points' `--env`/`--interpreter`
        selection, PRD-0003 AC8) so the merge logic is defined once.
        """
        raw_config = ws_context.ws_projects_raw_configs.get(p.dir_path, {})
        return project_env_universe_from_raw(raw_config)

    selections_by_project: dict[pathlib.Path, env_selection.EnvSelection] = {}
    env_universe_by_project: dict[pathlib.Path, dict[str, Any]] = {}
    try:
        for p in step_projects:
            env_universe = _project_env_universe(p)
            env_universe_by_project[p.dir_path] = env_universe
            selections_by_project[p.dir_path] = env_selection.resolve_env_selection(
                env_universe, env_names or [], interpreter_names or [], dev_env
            )
    except env_selection.EnvSelectionError as exc:
        raise PrepareEnvsFailed(str(exc)) from exc

    # Cross-project validation: an explicit --env/--interpreter selector must
    # match at least one env in *some* in-scope project (the pure resolver
    # tolerates an unmatched selector per-project — it can't know about
    # sibling projects).
    if env_names:
        for selector in env_names:
            if not any(
                env_selection.env_selector_known_in(selector, universe)
                for universe in env_universe_by_project.values()
            ):
                raise PrepareEnvsFailed(f"Unknown environment: '{selector}'")
    if interpreter_names:
        for selector in interpreter_names:
            if not any(
                env_selection.interpreter_selector_known_in(selector, universe)
                for universe in env_universe_by_project.values()
            ):
                raise PrepareEnvsFailed(f"Unknown interpreter: '{selector}'")

    # `create_envs`/`install_envs` run exclusively on each project's own
    # dev_workspace ER (never on other per-env ERs, which aren't even started
    # during prepare-envs — see the module-level "Verified 'projects' is the
    # right unit" note in the design). The subprocess fan-out they drive is
    # bounded by the machine-wide process budget (ADR-0090), leased by each
    # ER when the action run begins.
    await user_messages.info(f"Creating envs for {total_projects} project(s)...")

    create_errors: list[str] = []
    create_done = 0

    async def _create_one(p: domain.CollectedProject) -> None:
        nonlocal create_done
        sel = selections_by_project[p.dir_path]
        params = build_create_envs_params(
            sel, env_universe_by_project[p.dir_path], recreate
        )
        err = await _run_env_action(
            "fine_envs.CreateEnvsAction",
            params,
            p,
            ws_context,
            budget=fan_out_budget,
        )
        if err:
            create_errors.append(err)
        create_done += 1
        await user_messages.info(
            f"create_envs: {create_done}/{total_projects} project(s) done ({p.name})"
        )

    install_errors: list[str] = []
    install_done = 0

    async def _install_one(p: domain.CollectedProject) -> None:
        nonlocal install_done
        sel = selections_by_project[p.dir_path]
        if sel.active:
            install_env_names = sorted(sel.selected_env_names - {"dev_workspace"})
        else:
            install_env_names = sorted(
                name
                for name in env_universe_by_project[p.dir_path]
                if name != "dev_workspace"
            )
        # `dev_workspace` is excluded because step 4.5 already installed its
        # preset-resolved deps on the project's own now-running runner;
        # reinstalling it here would run from the env being replaced.
        params = {"env_names": install_env_names}
        err = await _run_env_action(
            "fine_envs.InstallEnvsAction",
            params,
            p,
            ws_context,
            # Step 6 reuses the step 5 fan-out's per-project share.
            budget=fan_out_budget,
        )
        if err:
            install_errors.append(err)
        install_done += 1
        await user_messages.info(
            f"install_envs: {install_done}/{total_projects} project(s) done ({p.name})"
        )

    await asyncio.gather(*[_create_one(p) for p in step_projects])
    if create_errors:
        raise PrepareEnvsFailed("'create_envs' failed:\n" + "\n".join(create_errors))

    # Step 6 — install_envs across all projects.
    logger.info("Installing dependencies...")
    await user_messages.info(
        f"Installing dependencies for {total_projects} project(s)..."
    )
    await asyncio.gather(*[_install_one(p) for p in step_projects])
    if install_errors:
        raise PrepareEnvsFailed("'install_envs' failed:\n" + "\n".join(install_errors))


async def install_env_for_project(
    project: domain.Project,
    env_name: str,
    ws_context: context.WorkspaceContext,
) -> None:
    """Install a specific environment for a project.

    Routing:
    - ``dev_workspace`` envs: delegated to the **workspace root's** dev_workspace runner,
      because the subproject's own runner does not exist yet.
    - All other envs: delegated to the **subproject's own** dev_workspace runner, which
      must be startable by the time a non-dev_workspace env is needed.

    In both cases the executor runner is started (or confirmed running) before the
    ``CreateEnvsAction`` + ``InstallEnvsAction`` pair is invoked.

    Args:
        project: The project whose environment needs to be installed.
        env_name: The name of the environment to install (e.g. ``"dev_no_runtime"``
            or ``"dev_workspace"``).
        ws_context: Workspace context.

    Raises:
        PrepareEnvsFailed: if the environment cannot be installed.
    """
    from finecode.wm_server.services import runner_start_service

    root_dir = ws_context.ws_dirs_paths[0]

    if env_name == "dev_workspace":
        # dev_workspace bootstrap: the subproject's own runner doesn't exist yet, so
        # delegate to the workspace root's runner.
        if project.dir_path == root_dir:
            # Cannot install the root project's dev_workspace via itself — circular.
            # Non-dev_workspace envs on the root project are safe because by then the
            # root dev_workspace runner is already running.
            venv_path = project.dir_path / ".venvs" / "dev_workspace"
            if not venv_path.exists():
                detail = f"venv directory does not exist ({venv_path})"
            else:
                runner = ws_context.ws_projects_extension_runners.get(
                    project.dir_path, {}
                ).get("dev_workspace")
                if runner is not None and runner.logs_path is not None:
                    detail = (
                        f"venv exists but runner failed to start "
                        f"(status: {runner.status.name}, logs: {runner.logs_path})"
                    )
                else:
                    detail = "venv exists but runner could not start"
            raise PrepareEnvsFailed(
                f"Cannot auto-install env 'dev_workspace' for the workspace root project: "
                f"{detail}. Run `finecode prepare-envs` to set up the environment."
            )

        root_project = ws_context.ws_projects.get(root_dir)
        if root_project is None:
            raise PrepareEnvsFailed(
                f"Root project not found — cannot install dev_workspace for '{project.name}'"
            )

        # Serialize concurrent root-runner initializations so that canonical_source
        # values are populated before the env-creation calls below.
        # Use env_install_locks (not project_init_locks) to avoid deadlocking
        # with _handle_add_dir / _handle_start_runners, which hold project_init_locks
        # for the entire slow startup phase and call into this function indirectly
        # via _auto_prepare_and_retry.
        root_init_lock = ws_context.env_install_locks.get(root_dir)
        if root_init_lock is None:
            root_init_lock = asyncio.Lock()
            ws_context.env_install_locks[root_dir] = root_init_lock

        async with root_init_lock:
            root_project = ws_context.ws_projects.get(root_dir)
            try:
                await runner_start_service.start_runners_with_auto_prepare(
                    [root_project], ws_context
                )
                root_project = ws_context.ws_projects.get(root_dir)
            except Exception as exc:
                raise PrepareEnvsFailed(
                    f"Root project runner not available — cannot install dev_workspace"
                    f" for '{project.name}': {exc}"
                ) from exc

            if not isinstance(root_project, domain.CollectedProject):
                raise PrepareEnvsFailed(
                    f"Root project not ready — cannot install dev_workspace for '{project.name}'"
                )

        executor_project: domain.CollectedProject = root_project
    else:
        # Non-dev_workspace env: use the subproject's own dev_workspace runner.
        # Its dev_workspace must already exist (it was set up before any other envs
        # are created), so starting it here is safe and idempotent.
        project_init_lock = ws_context.env_install_locks.get(project.dir_path)
        if project_init_lock is None:
            project_init_lock = asyncio.Lock()
            ws_context.env_install_locks[project.dir_path] = project_init_lock

        async with project_init_lock:
            try:
                await runner_start_service.start_runners_with_auto_prepare(
                    [project], ws_context
                )
            except Exception as exc:
                raise PrepareEnvsFailed(
                    f"Project runner for '{project.name}' not available — cannot install"
                    f" env '{env_name}': {exc}"
                ) from exc
            resolved = ws_context.ws_projects.get(project.dir_path)

        if not isinstance(resolved, domain.CollectedProject):
            raise PrepareEnvsFailed(
                f"Project '{project.name}' not ready — cannot install env '{env_name}'"
            )
        executor_project = resolved

    env_spec: dict[str, str] = {
        "name": env_name,
        "venv_dir_path": (project.dir_path / ".venvs" / env_name).as_uri(),
        "project_def_path": (project.dir_path / "pyproject.toml").as_uri(),
    }
    interpreter = (
        ws_context.ws_projects_raw_configs.get(project.dir_path, {})
        .get("tool", {})
        .get("finecode", {})
        .get("env", {})
        .get(env_name, {})
        .get("interpreter")
    )
    if interpreter is not None:
        env_spec["interpreter"] = interpreter

    # Create the venv if it does not exist yet (idempotent on existing venvs).
    error = await _run_env_action(
        "fine_envs.CreateEnvsAction",
        {"envs": [env_spec]},
        executor_project,
        ws_context,
        # Auto-prepare runs inside another dispatch, so it must never wait on
        # slots its own ancestor holds — the escape is load-bearing.
        budget=domain.RunBudget(),
    )
    if error:
        raise PrepareEnvsFailed(
            f"create_envs failed for env '{env_name}' in '{project.name}': {error}"
        )

    error = await _run_env_action(
        "fine_envs.InstallEnvsAction",
        {"envs": [env_spec]},
        executor_project,
        ws_context,
        # Same load-bearing escape as the create above.
        budget=domain.RunBudget(),
    )
    if error:
        raise PrepareEnvsFailed(
            f"install_envs failed for env '{env_name}' in '{project.name}': {error}"
        )


__all__ = [
    "PrepareEnvsFailed",
    "build_create_envs_params",
    "install_env_for_project",
    "prepare_envs",
]
