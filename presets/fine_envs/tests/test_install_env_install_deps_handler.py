import pathlib
from typing import Any

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectinfoprovider

from fine_envs import (
    install_deps_in_env_action,
    install_env_action,
)
from fine_envs.create_envs_action import EnvInfo
from fine_envs.install_env_install_deps_handler import InstallEnvInstallDepsHandler


class _FakeActionRunner:
    def __init__(self) -> None:
        self.payloads: list[install_deps_in_env_action.InstallDepsInEnvRunPayload] = []

    async def run_action(
        self,
        action_type: Any,
        payload: install_deps_in_env_action.InstallDepsInEnvRunPayload,
        meta: code_action.RunActionMeta,
        caller_kwargs: code_action.CallerRunContextKwargs | None = None,
    ) -> install_deps_in_env_action.InstallDepsInEnvRunResult:
        self.payloads.append(payload)
        return install_deps_in_env_action.InstallDepsInEnvRunResult(errors=[])


class _FakeProjectInfoProvider:
    def __init__(
        self, ws_workspace_packages: dict[str, iprojectinfoprovider.WorkspacePackage]
    ) -> None:
        self._ws_workspace_packages = ws_workspace_packages

    async def get_workspace_packages(
        self,
    ) -> dict[str, iprojectinfoprovider.WorkspacePackage]:
        return self._ws_workspace_packages


class _FakeLogger:
    def debug(self, message: str) -> None: ...


def _make_env(
    tmp_path: pathlib.Path,
    *,
    override: list[str] | None = None,
) -> EnvInfo:
    return EnvInfo(
        name="dev",
        venv_dir_path=(tmp_path / ".venvs" / "dev").as_uri(),
        project_def_path=(tmp_path / "pyproject.toml").as_uri(),
        dependencies_override=override or [],
    )


def _make_project_def(
    env_deps: list[str],
    *,
    project_name: str | None = None,
    install_project: bool = False,
) -> dict[str, Any]:
    project_def: dict[str, Any] = {"dependency-groups": {"dev": env_deps}}
    if project_name is not None:
        project_def["project"] = {"name": project_name}
    if install_project:
        project_def.setdefault("tool", {}).setdefault("finecode", {}).setdefault(
            "env", {}
        )["dev"] = {"install_project": True}
    return project_def


async def _run_handler(
    tmp_path: pathlib.Path,
    project_def: dict[str, Any],
    override: list[str] | None = None,
    ws_packages: dict[str, iprojectinfoprovider.WorkspacePackage] | None = None,
) -> list[install_deps_in_env_action.Dependency]:
    action_runner = _FakeActionRunner()
    handler = InstallEnvInstallDepsHandler(
        action_runner=action_runner,
        logger=_FakeLogger(),
        project_info_provider=_FakeProjectInfoProvider(ws_packages or {}),
    )
    env = _make_env(tmp_path, override=override)
    payload = install_env_action.InstallEnvRunPayload(env=env)
    run_context = install_env_action.InstallEnvRunContext(
        run_id=1,
        initial_payload=payload,
        meta=code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.SYSTEM,
            dev_env=code_action.DevEnv.CI,
        ),
        info_provider=None,  # type: ignore[arg-type]
    )
    run_context.project_def = project_def

    await handler.run(payload, run_context)

    assert len(action_runner.payloads) == 1
    return action_runner.payloads[0].dependencies


async def test_override_with_extras_puts_extras_in_field_not_version(
    tmp_path: pathlib.Path,
) -> None:
    """An override spec's bracket group lands in the extras field, not in the
    version specifier.

    If the bracket group stayed in ``version_or_source`` the backend would emit
    a malformed requirement (or silently drop the extras).
    """
    project_def = _make_project_def(["pyrefly~=1.0"])

    deps = await _run_handler(tmp_path, project_def, override=["pyrefly[a]==1.2.*"])

    assert len(deps) == 1
    assert deps[0].name == "pyrefly"
    assert deps[0].extras == ["a"]
    assert deps[0].version_or_source == "==1.2.*"


async def test_override_without_extras_replaces_existing_extras(
    tmp_path: pathlib.Path,
) -> None:
    """An override without a bracket group replaces, not merges, the extras the
    overridden spec carried.
    """
    project_def = _make_project_def(["pyrefly[x]~=1.0"])

    deps = await _run_handler(tmp_path, project_def, override=["pyrefly==1.3.*"])

    assert deps[0].extras == []
    assert deps[0].version_or_source == "==1.2.*"


async def test_override_adding_new_dep_carries_extras_key(
    tmp_path: pathlib.Path,
) -> None:
    """The not-replaced override branch still produces a dep dict with extras."""
    project_def = _make_project_def(["other~=1.0"])

    deps = await _run_handler(tmp_path, project_def, override=["pyrefly[a]==1.2.*"])

    names = {dep.name for dep in deps}
    assert names == {"other", "pyrefly"}
    pyrefly = next(dep for dep in deps if dep.name == "pyrefly")
    assert pyrefly.extras == ["a"]


async def test_install_project_preserves_extras_on_replaced_entry(
    tmp_path: pathlib.Path,
) -> None:
    """install_project's editable replacement keeps the extras of the entry it
    displaces.

    The project is still installed exactly once (ADR-0046), but dropping the
    extras here would make a selected extra silently not install its packages.
    """
    project_def = _make_project_def(
        ["my_project[x]~=1.0"], project_name="my_project", install_project=True
    )

    deps = await _run_handler(tmp_path, project_def)

    assert len(deps) == 1
    assert deps[0].name == "my_project"
    assert deps[0].editable is True
    assert deps[0].extras == ["x"]
    assert deps[0].version_or_source == f" @ file://{tmp_path.as_posix()}"


async def test_workspace_package_installs_from_wheel_in_wheel_mode(
    tmp_path: pathlib.Path,
) -> None:
    """A workspace package with a built wheel is installed from that wheel, not
    its source directory, so wheel-mode envs test the built artifact."""
    wheel = tmp_path / "my_project-1.0.0-py3-none-any.whl"
    project_def = _make_project_def(["my_project~=1.0"])

    deps = await _run_handler(
        tmp_path,
        project_def,
        ws_packages={
            "my_project": iprojectinfoprovider.WorkspacePackage(
                dir=tmp_path, wheel=wheel, editable=False
            )
        },
    )

    assert deps[0].version_or_source == f" @ file://{wheel.as_posix()}"
    assert deps[0].editable is False


async def test_workspace_package_installs_editable_without_wheel(
    tmp_path: pathlib.Path,
) -> None:
    """The default editable mode (no wheel in the map) still installs from the
    package's source directory, preserving today's local workflow."""
    project_def = _make_project_def(["my_project~=1.0"])

    deps = await _run_handler(
        tmp_path,
        project_def,
        ws_packages={"my_project": iprojectinfoprovider.WorkspacePackage(dir=tmp_path)},
    )

    assert deps[0].version_or_source == f" @ file://{tmp_path.as_posix()}"
    assert deps[0].editable is True
