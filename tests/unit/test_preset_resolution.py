import pathlib

import pytest

from finecode.wm_server import context, domain
from finecode.wm_server.config import config_models
from finecode.wm_server.runner import preset_resolution
from finecode.wm_server.runner.runner_client import BaseRunnerRequestException


class _FakeClient:
    async def send_request(
        self, method: str, params: object, timeout: float | None = None
    ) -> dict:
        raise BaseRunnerRequestException("cannot find package 'fine_lint_fix'")


class _FakeRunner:
    def __init__(self) -> None:
        self.client = _FakeClient()


def _gated_preset() -> preset_resolution.PresetToProcess:
    return preset_resolution.PresetToProcess(
        source="fine_lint_fix",
        project_def_path=pathlib.Path("/ws/project/pyproject.toml"),
        gated_by_extra="lint_fix",
        gated_by_package="finecode_dev_common_preset",
    )


async def test_gated_missing_clone_error_names_selection_file() -> None:
    """A selected extra whose repository is absent produces an error that names
    the selection file and the extra, not a bare resolution failure."""
    ws_context = context.WorkspaceContext(ws_dirs_paths=[])

    with pytest.raises(config_models.PresetPackageNotInstalledError) as exc:
        await preset_resolution.get_preset_project_path(
            _gated_preset(),
            pathlib.Path("/ws/project/pyproject.toml"),
            _FakeRunner(),
            ws_context,
        )

    message = str(exc.value)
    assert "finecode-workspace-user.toml" in message
    assert "lint_fix" in message
    assert "Clone it" in message


async def test_gated_clone_present_but_not_installed(tmp_path: pathlib.Path) -> None:
    """When the clone is a discovered workspace project, the remediation is to
    re-run prepare-envs rather than to clone the repository."""
    ws_context = context.WorkspaceContext(ws_dirs_paths=[])
    project = domain.Project(
        name="fine_lint_fix",
        dir_path=tmp_path,
        def_path=tmp_path / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
    )
    ws_context.ws_projects[tmp_path] = project

    with pytest.raises(config_models.PresetPackageNotInstalledError) as exc:
        await preset_resolution.get_preset_project_path(
            _gated_preset(),
            pathlib.Path("/ws/project/pyproject.toml"),
            _FakeRunner(),
            ws_context,
        )

    assert "prepare-envs" in str(exc.value)


async def test_non_gated_preset_keeps_legacy_message() -> None:
    """An ordinary tracked preset keeps the existing remediation text."""
    ws_context = context.WorkspaceContext(ws_dirs_paths=[])
    preset = preset_resolution.PresetToProcess(
        source="fine_lint_fix",
        project_def_path=pathlib.Path("/ws/project/pyproject.toml"),
        declared_by="finecode_dev_common_preset",
    )

    with pytest.raises(config_models.PresetPackageNotInstalledError) as exc:
        await preset_resolution.get_preset_project_path(
            preset,
            pathlib.Path("/ws/project/pyproject.toml"),
            _FakeRunner(),
            ws_context,
        )

    assert (
        "Add 'fine_lint_fix' to the pip dependencies of 'finecode_dev_common_preset'"
        in str(exc.value)
    )
