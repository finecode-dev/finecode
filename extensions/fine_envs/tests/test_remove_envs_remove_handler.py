import pathlib
import stat

import pytest
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import ifilemanager
from finecode_extension_api.resource_uri import path_to_resource_uri
from finecode_extension_runner.impls.file_manager import FileManager

from fine_envs import remove_envs_action
from fine_envs.create_envs_action import EnvInfo
from fine_envs.remove_envs_remove_handler import RemoveEnvsRemoveHandler


class _FakeLogger:
    def debug(self, message: str) -> None: ...

    def info(self, message: str) -> None: ...

    def warning(self, message: str) -> None: ...


class _FailForNamesFileManager:
    """Wraps a real `FileManager`, forcing `remove_dir` to fail for the given
    venv names so `remove_envs`'s per-env error handling can be exercised.

    The failing branch raises `RemoveDirError`, as a real `IFileManager` does
    for a removal it cannot perform."""

    def __init__(self, failing_names: set[str]) -> None:
        self._real = FileManager(logger=_FakeLogger())
        self._failing_names = failing_names

    async def remove_dir(
        self, dir_path: pathlib.Path, *, tolerant: bool = False
    ) -> None:
        if dir_path.name in self._failing_names:
            raise ifilemanager.RemoveDirError("device or resource busy")
        await self._real.remove_dir(dir_path, tolerant=tolerant)


def _make_run_context(
    envs: list[EnvInfo] | None,
) -> remove_envs_action.RemoveEnvsRunContext:
    payload = remove_envs_action.RemoveEnvsRunPayload()
    run_context = remove_envs_action.RemoveEnvsRunContext(
        run_id=1,
        initial_payload=payload,
        meta=code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.USER,
            dev_env=code_action.DevEnv.CLI,
        ),
        info_provider=None,  # type: ignore[arg-type]
    )
    run_context.envs = envs
    return run_context


def _env_info(project_dir: pathlib.Path, name: str) -> EnvInfo:
    return EnvInfo(
        name=name,
        venv_dir_path=path_to_resource_uri(project_dir / ".venvs" / name),
        project_def_path=path_to_resource_uri(project_dir / "pyproject.toml"),
    )


def _make_venv(venvs_dir: pathlib.Path, name: str) -> pathlib.Path:
    venv_dir = venvs_dir / name
    venv_dir.mkdir(parents=True)
    (venv_dir / "pyvenv.cfg").write_text("home = /usr/bin\n")
    return venv_dir


async def test_removes_env_dir(tmp_path: pathlib.Path) -> None:
    venv_dir = _make_venv(tmp_path / ".venvs", "testing")
    run_context = _make_run_context([_env_info(tmp_path, "testing")])

    result = await RemoveEnvsRemoveHandler(
        file_manager=FileManager(logger=_FakeLogger()), logger=_FakeLogger()
    ).run(remove_envs_action.RemoveEnvsRunPayload(), run_context)

    assert not venv_dir.exists()
    assert result.removed == ["testing"]
    assert result.errors == []
    assert result.return_code == code_action.RunReturnCode.SUCCESS


async def test_removes_broken_env_with_read_only_contents(
    tmp_path: pathlib.Path,
) -> None:
    """Removing fully even when broken is the point: a venv that lost its
    pyvenv.cfg and has permission-stripped contents must still go."""
    venvs_dir = tmp_path / ".venvs"
    venv_dir = venvs_dir / "testing"
    nested = venv_dir / "lib" / "site-packages"
    nested.mkdir(parents=True)
    locked = nested / "locked.py"
    locked.write_text("x = 1\n")
    locked.chmod(stat.S_IRUSR)
    nested.chmod(stat.S_IRUSR | stat.S_IXUSR)

    run_context = _make_run_context([_env_info(tmp_path, "testing")])

    result = await RemoveEnvsRemoveHandler(
        file_manager=FileManager(logger=_FakeLogger()), logger=_FakeLogger()
    ).run(remove_envs_action.RemoveEnvsRunPayload(), run_context)

    assert not venv_dir.exists()
    assert result.removed == ["testing"]


async def test_one_failure_does_not_abort_the_batch(tmp_path: pathlib.Path) -> None:
    _make_venv(tmp_path / ".venvs", "broken")
    good_venv = _make_venv(tmp_path / ".venvs", "good")

    run_context = _make_run_context(
        [_env_info(tmp_path, "broken"), _env_info(tmp_path, "good")]
    )

    result = await RemoveEnvsRemoveHandler(
        file_manager=_FailForNamesFileManager({"broken"}), logger=_FakeLogger()
    ).run(remove_envs_action.RemoveEnvsRunPayload(), run_context)

    assert not good_venv.exists()
    assert result.removed == ["good"]
    assert len(result.errors) == 1
    assert "broken" in result.errors[0]
    assert result.return_code == code_action.RunReturnCode.ERROR


async def test_undiscovered_envs_is_a_contract_error(tmp_path: pathlib.Path) -> None:
    run_context = _make_run_context(None)

    with pytest.raises(code_action.ActionFailedException):
        await RemoveEnvsRemoveHandler(
            file_manager=FileManager(logger=_FakeLogger()), logger=_FakeLogger()
        ).run(remove_envs_action.RemoveEnvsRunPayload(), run_context)
