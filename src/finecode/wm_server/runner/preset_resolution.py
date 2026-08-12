"""Resolves a project's py-preset contributions through an already-running
dev_workspace runner.

This is the runner-dependent half of reading a project's config: reading and merging
config is pure and belongs in ``config/``, but resolving where an installed preset
package lives on disk means asking a live Extension Runner
(``finecode/resolvePackagePath``), which is the runner layer's job. Because the halves
are separate, neither layer reaches into the other's types —
``config.read_configs.read_project_config_sources`` / ``finish_project_config`` stay
runner-agnostic, and this module uses ``runner_client.ExtensionRunnerInfo`` directly
since it lives where that type does.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple

from loguru import logger

from finecode import user_messages
from finecode.wm_server import context, domain
from finecode.wm_server.config import config_models, read_configs
from finecode.wm_server.runner import runner_client


class PresetToProcess(NamedTuple):
    source: str
    project_def_path: Path
    declared_by: str | None = None  # None means declared directly by the project


async def get_preset_project_path(
    preset: PresetToProcess, def_path: Path, runner: runner_client.ExtensionRunnerInfo
) -> Path:
    """Ask *runner* where the preset package named by *preset* lives on disk.

    Raises:
        PresetPackageNotInstalledError: the package is not installed in the
            dev_workspace environment.
        ConfigurationError: the runner failed to resolve the path for any other
            reason, or answered without a package path.
    """
    logger.trace(f"Get preset project path: {preset.source}")

    try:
        resolve_path_result = await runner_client.resolve_package_path(
            runner, preset.source
        )
    except runner_client.BaseRunnerRequestException as error:
        error_message = error.message
        lower_message = error_message.lower()
        if "cannot find package" in lower_message or "no module named" in lower_message:
            if preset.declared_by is not None:
                description = (
                    f"Preset '{preset.source}' is declared by preset '{preset.declared_by}' "
                    f"(used in project {def_path.parent}) "
                    f"but '{preset.source}' is not installed in the dev_workspace environment. "
                    f"Add '{preset.source}' to the pip dependencies of '{preset.declared_by}' "
                    f"in its pyproject.toml, then re-run 'prepare-envs'."
                )
            else:
                description = (
                    f"Preset '{preset.source}' is declared in project {def_path.parent} "
                    f"but is not installed in the dev_workspace environment. "
                    f"Add '{preset.source}' to the project's dev_workspace pip dependencies "
                    f"in its pyproject.toml, then re-run 'prepare-envs'."
                )
            raise config_models.PresetPackageNotInstalledError(description)

        await user_messages.error(f"Failed to get preset project path: {error_message}")
        raise config_models.ConfigurationError(
            "Failed to resolve preset package path "
            f"for {preset.source} in project {def_path.parent}: {error_message}"
        )
    try:
        preset_project_path = Path(resolve_path_result["packagePath"])
    except KeyError as exception:
        raise config_models.ConfigurationError(
            f"Preset source cannot be resolved — ER response missing 'packagePath': {preset.source}"
        ) from exception

    logger.trace(f"Got: {preset.source} -> {preset_project_path}")
    return preset_project_path


async def collect_config_from_py_presets(
    presets_sources: list[str],
    def_path: Path,
    runner: runner_client.ExtensionRunnerInfo,
) -> dict[str, Any] | None:
    config: dict[str, Any] | None = None
    processed_presets: set[str] = set()
    presets_to_process: set[PresetToProcess] = {
        PresetToProcess(source=preset_source, project_def_path=def_path)
        for preset_source in presets_sources
    }
    while len(presets_to_process) > 0:
        preset = presets_to_process.pop()
        processed_presets.add(preset.source)

        preset_project_path = await get_preset_project_path(
            preset=preset, def_path=def_path, runner=runner
        )

        preset_toml_path = preset_project_path / "preset.toml"
        preset_toml, preset_config = read_configs.read_preset_config(
            preset_toml_path, preset.source
        )
        if config is None:
            # use merge instead of just assigning config, because merge not only merges
            # configs, but also adapts relative pathes etc.
            config = {}
        read_configs.merge_projects_configs(
            config, def_path, preset_toml, preset_toml_path, is_from_preset=True
        )
        new_presets_sources = {
            extend.source for extend in preset_config.extends
        } - processed_presets
        for new_preset_source in new_presets_sources:
            presets_to_process.add(
                PresetToProcess(
                    source=new_preset_source,
                    project_def_path=def_path,
                    declared_by=preset.source,
                )
            )

    return config


async def read_project_config_with_py_presets(
    project: domain.Project,
    ws_context: context.WorkspaceContext,
    resolve_presets: bool = True,
) -> None:
    """Read a project's config, resolving any py-preset contributions through its
    dev_workspace runner if one is already running in *ws_context*. Wraps
    ``config.read_configs``' pure read/merge split around the one RPC round trip
    that split exists to keep out of the config layer.

    Raises:
        PresetPackageNotInstalledError: a declared preset package is not installed
            in the dev_workspace environment.
        ConfigurationError: the project's config files are malformed, or a preset
            package path could not be resolved.
    """
    sources = read_configs.read_project_config_sources(project, resolve_presets)
    if sources is None:
        return

    # TODO: can it be the case that there is no such runner?
    dev_workspace_runner = ws_context.ws_projects_extension_runners.get(
        project.dir_path, {}
    ).get("dev_workspace")

    py_presets_config: dict[str, Any] | None = None
    if dev_workspace_runner is not None:
        py_presets_config = await collect_config_from_py_presets(
            presets_sources=sources.preset_sources,
            def_path=project.def_path,
            runner=dev_workspace_runner,
        )

    read_configs.finish_project_config(project, ws_context, sources, py_presets_config)
