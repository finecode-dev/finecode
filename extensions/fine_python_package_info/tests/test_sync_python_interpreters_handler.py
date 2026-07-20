from __future__ import annotations

import pathlib

import pytest
import tomlkit
from finecode_extension_api import code_action
from finecode_extension_api.interfaces.ifileeditor import IFileEditor
from finecode_extension_api.interfaces.ilogger import ILogger
from finecode_extension_api.interfaces.iprojectinfoprovider import (
    IProjectInfoProvider,
)

# the ER wraps a handler's code_action.ActionFailedException in its own exception type
# before it reaches the caller, so that is what a session-level failure asserts on
from finecode_extension_runner._services.run_action import (
    ActionFailedException as ActionRunFailed,
)
from finecode_extension_runner.testing import InMemoryFileEditor, handler_test_session

from fine_python_lang.list_obtainable_python_interpreters_action import (
    ListObtainablePythonInterpretersAction,
)
from fine_python_lang.sync_python_interpreters_action import (
    SyncPythonInterpretersAction,
    SyncPythonInterpretersRunPayload,
)
from fine_python_package_info.sync_python_interpreters_handler import (
    SyncPythonInterpretersHandler,
    derive_interpreters,
)

from tests.stubs import OBTAINABLE as _OBTAINABLE
from tests.stubs import CollectingLogger, StubObtainableInterpretersHandler

_ACTION_NAME = SyncPythonInterpretersAction.__name__
_ACTION_SOURCE = (
    f"{SyncPythonInterpretersAction.__module__}.{SyncPythonInterpretersAction.__qualname__}"
)
_HANDLER_NAME = SyncPythonInterpretersHandler.__name__
_HANDLER_SOURCE = (
    f"{SyncPythonInterpretersHandler.__module__}"
    f".{SyncPythonInterpretersHandler.__qualname__}"
)


_OBTAINABLE_ACTION_NAME = ListObtainablePythonInterpretersAction.__name__
_OBTAINABLE_ACTION_SOURCE = (
    f"{ListObtainablePythonInterpretersAction.__module__}"
    f".{ListObtainablePythonInterpretersAction.__qualname__}"
)
_STUB_SOURCE = (
    f"{StubObtainableInterpretersHandler.__module__}"
    f".{StubObtainableInterpretersHandler.__qualname__}"
)


# the runner only executes a nested action in-process when every handler for it is
# registered in the session's env, so both handlers must name it explicitly
_SESSION_ENV = "test"


def _actions(**handler_config) -> dict:
    return {
        _ACTION_NAME: {
            "source": _ACTION_SOURCE,
            "handlers": [
                {
                    "name": _HANDLER_NAME,
                    "source": _HANDLER_SOURCE,
                    "config": handler_config,
                    "env": _SESSION_ENV,
                }
            ],
        },
        # the deriving handler asks the provisioner what it can obtain; stub that so the
        # test does not depend on this machine's uv
        _OBTAINABLE_ACTION_NAME: {
            "source": _OBTAINABLE_ACTION_SOURCE,
            "handlers": [
                {
                    "name": "stub_obtainable",
                    "source": _STUB_SOURCE,
                    "env": _SESSION_ENV,
                }
            ],
        },
    }


def _write_pyproject(
    project_dir: pathlib.Path,
    requires_python: str = ">=3.11",
    env_table: str = "",
) -> pathlib.Path:
    project_def_path = project_dir / "pyproject.toml"
    project_def_path.write_text(
        "[project]\n"
        'name = "sample"\n'
        f'requires-python = "{requires_python}"\n'
        "\n"
        "[dependency-groups]\n"
        'dev = ["pytest"]\n' + env_table
    )
    return project_def_path


# --- derivation -------------------------------------------------------------


def test_bounded_range_expands_to_its_members() -> None:
    assert derive_interpreters(">=3.11,<3.14", _OBTAINABLE) == [
        "cpython@3.11",
        "cpython@3.12",
        "cpython@3.13",
    ]


def test_open_upper_bound_is_bounded_by_what_is_obtainable() -> None:
    # the correct form for a published package: no upper bound at all. It cannot be
    # expanded from the specifier alone, which is why the result is persisted.
    assert derive_interpreters(">=3.13", _OBTAINABLE) == [
        "cpython@3.13",
        "cpython@3.14",
    ]


def test_non_cpython_is_never_derived() -> None:
    # requires-python constrains version only and carries no implementation, so PyPy and
    # GraalPy must not appear even though the provisioner offers them
    assert derive_interpreters(">=3.10", _OBTAINABLE) == [
        "cpython@3.10",
        "cpython@3.11",
        "cpython@3.12",
        "cpython@3.13",
        "cpython@3.14",
    ]


def test_ceiling_caps_the_newest_derived_version() -> None:
    assert derive_interpreters(
        ">=3.12", _OBTAINABLE, max_supported_python="3.12"
    ) == ["cpython@3.12"]


def test_extra_interpreters_are_appended_after_the_derived_rows() -> None:
    assert derive_interpreters(
        ">=3.14", _OBTAINABLE, extra_interpreters=["pypy@3.11"]
    ) == ["cpython@3.14", "pypy@3.11"]


def test_extra_interpreter_already_derived_is_not_duplicated() -> None:
    assert derive_interpreters(
        ">=3.14", _OBTAINABLE, extra_interpreters=["3.14"]
    ) == ["cpython@3.14"]


def test_patch_level_floor_keeps_its_own_minor() -> None:
    # regression: an axis row is a minor series, but what create_env provisions for it is
    # uv's newest patch. Matching the bare string "3.11" against ">=3.11.4" is always
    # False, which silently dropped a project's own minimum supported minor.
    assert derive_interpreters(">=3.11.4", _OBTAINABLE) == [
        "cpython@3.11",
        "cpython@3.12",
        "cpython@3.13",
        "cpython@3.14",
    ]


def test_patch_level_ceiling_drops_the_minor_it_cannot_satisfy() -> None:
    # the other side of the same rule: uv would install 3.11.15 for a `cpython@3.11` row,
    # which violates <3.11.5, so the row would yield an env the project cannot use
    assert "cpython@3.11" not in derive_interpreters(">=3.10,<3.11.5", _OBTAINABLE)


def test_invalid_max_supported_python_is_an_action_failure() -> None:
    # a typo'd handler config must name the offending key, not surface as a raw
    # packaging.InvalidVersion traceback
    with pytest.raises(
        code_action.ActionFailedException, match="max_supported_python"
    ):
        derive_interpreters(">=3.11", _OBTAINABLE, max_supported_python="3.12.x")


def test_specifier_matching_nothing_obtainable_is_an_error() -> None:
    with pytest.raises(code_action.ActionFailedException, match="no obtainable"):
        derive_interpreters(">=3.99", _OBTAINABLE)


def test_invalid_specifier_is_an_error() -> None:
    with pytest.raises(code_action.ActionFailedException):
        derive_interpreters("three point eleven", _OBTAINABLE)


class _StubProjectInfoProvider:
    """Reports a merged config independent of what's on disk at *project_dir*.

    Stands in for the real provider's ``get_project_raw_config``, which returns the
    preset-merged config -- exercising envs that a preset declares entirely on its own,
    with nothing written into the project's own pyproject.toml.
    """

    def __init__(self, project_dir: pathlib.Path, merged_config: dict) -> None:
        self._project_dir = project_dir
        self._merged_config = merged_config

    def get_current_project_dir_path(self) -> pathlib.Path:
        return self._project_dir

    def get_current_project_def_path(self) -> pathlib.Path:
        return self._project_dir / "pyproject.toml"

    async def get_current_project_package_name(self) -> str:
        raise NotImplementedError

    async def get_project_raw_config(self, project_def_path: pathlib.Path) -> dict:
        return self._merged_config

    async def get_current_project_raw_config(self) -> dict:
        return self._merged_config

    def get_current_project_raw_config_version(self) -> int:
        return 0

    async def get_workspace_editable_packages(self) -> dict:
        return {}


# --- handler ----------------------------------------------------------------


async def test_writes_derived_axis_into_the_env_table(tmp_path: pathlib.Path) -> None:
    project_def_path = _write_pyproject(tmp_path, requires_python=">=3.12,<3.14")
    file_editor = InMemoryFileEditor()

    async with handler_test_session(
        project_dir=tmp_path,
        actions=_actions(envs=["dev"]),
        service_overrides={IFileEditor: file_editor},
    ) as session:
        result = await session.run_action(
            _ACTION_NAME, SyncPythonInterpretersRunPayload(save=True)
        )

    assert result.saved is True
    written = tomlkit.parse(file_editor.contents(project_def_path))
    assert written["tool"]["finecode"]["env"]["dev"]["interpreters"] == [
        "cpython@3.12",
        "cpython@3.13",
    ]


async def test_save_false_reports_drift_without_writing(tmp_path: pathlib.Path) -> None:
    _write_pyproject(tmp_path, requires_python=">=3.12,<3.14")
    file_editor = InMemoryFileEditor()

    async with handler_test_session(
        project_dir=tmp_path,
        actions=_actions(envs=["dev"]),
        service_overrides={IFileEditor: file_editor},
    ) as session:
        result = await session.run_action(
            _ACTION_NAME, SyncPythonInterpretersRunPayload(save=False)
        )

    assert result.saved is False
    assert file_editor.writes == []
    assert [axis.env_name for axis in result.axes if axis.changed] == ["dev"]


async def test_axis_already_current_is_left_alone(tmp_path: pathlib.Path) -> None:
    _write_pyproject(
        tmp_path,
        requires_python=">=3.13,<3.14",
        env_table=(
            "\n[tool.finecode.env.dev]\n" 'interpreters = ["cpython@3.13"]\n'
        ),
    )
    file_editor = InMemoryFileEditor()

    async with handler_test_session(
        project_dir=tmp_path,
        actions=_actions(envs=["dev"]),
        service_overrides={IFileEditor: file_editor},
    ) as session:
        result = await session.run_action(
            _ACTION_NAME, SyncPythonInterpretersRunPayload(save=True)
        )

    assert result.saved is False
    assert file_editor.writes == []
    assert [axis.changed for axis in result.axes] == [False]


async def test_version_only_shorthand_is_not_drift(tmp_path: pathlib.Path) -> None:
    # ADR-0047: "3.13" and "cpython@3.13" are the same identity, so a project that
    # spelled the axis the short way must not be reported as out of date.
    _write_pyproject(
        tmp_path,
        requires_python=">=3.13,<3.14",
        env_table="\n[tool.finecode.env.dev]\ninterpreters = [\"3.13\"]\n",
    )
    file_editor = InMemoryFileEditor()

    async with handler_test_session(
        project_dir=tmp_path,
        actions=_actions(envs=["dev"]),
        service_overrides={IFileEditor: file_editor},
    ) as session:
        result = await session.run_action(
            _ACTION_NAME, SyncPythonInterpretersRunPayload(save=True)
        )

    assert result.saved is False
    assert file_editor.writes == []


async def test_no_configured_envs_is_a_no_op(tmp_path: pathlib.Path) -> None:
    # matrices stay opt-in (PRD-0003 R7)
    _write_pyproject(tmp_path)
    file_editor = InMemoryFileEditor()

    async with handler_test_session(
        project_dir=tmp_path,
        actions=_actions(),
        service_overrides={IFileEditor: file_editor},
    ) as session:
        result = await session.run_action(
            _ACTION_NAME, SyncPythonInterpretersRunPayload(save=True)
        )

    assert result.axes == []
    assert file_editor.writes == []


async def test_undeclared_env_is_an_error(tmp_path: pathlib.Path) -> None:
    _write_pyproject(tmp_path)

    async with handler_test_session(
        project_dir=tmp_path,
        actions=_actions(envs=["nope"]),
        service_overrides={IFileEditor: InMemoryFileEditor()},
    ) as session:
        with pytest.raises(ActionRunFailed, match="not declared"):
            await session.run_action(
                _ACTION_NAME, SyncPythonInterpretersRunPayload(save=True)
            )


async def test_env_declared_only_via_preset_merged_config_is_accepted(
    tmp_path: pathlib.Path,
) -> None:
    # A matrix env like `testing` can be declared entirely by a preset -- nothing
    # written into this project's own pyproject.toml declares it. The merged config,
    # not the raw file, is what proves the env is real (regression test).
    _write_pyproject(tmp_path, requires_python=">=3.12,<3.14")
    file_editor = InMemoryFileEditor()
    merged_config = {"dependency-groups": {"testing": ["pytest"]}}

    async with handler_test_session(
        project_dir=tmp_path,
        actions=_actions(envs=["testing"]),
        service_overrides={
            IFileEditor: file_editor,
            IProjectInfoProvider: _StubProjectInfoProvider(tmp_path, merged_config),
        },
    ) as session:
        result = await session.run_action(
            _ACTION_NAME, SyncPythonInterpretersRunPayload(save=True)
        )

    assert result.saved is True
    written = tomlkit.parse(file_editor.contents(tmp_path / "pyproject.toml"))
    assert written["tool"]["finecode"]["env"]["testing"]["interpreters"] == [
        "cpython@3.12",
        "cpython@3.13",
    ]


async def test_env_already_expanded_into_matrix_children_is_accepted(
    tmp_path: pathlib.Path,
) -> None:
    # Once a matrix env's axis is known, config resolution expands the base name into
    # one concrete env per interpreter (`<name>@<impl>-<version>`, ADR-0047) -- the bare
    # base name then no longer appears as a key in the merged config at all. A run right
    # after the axis was first derived and written must not treat the env as undeclared
    # (regression test).
    _write_pyproject(
        tmp_path,
        requires_python=">=3.12,<3.14",
        env_table=(
            "\n[tool.finecode.env.testing]\n"
            'interpreters = ["cpython@3.12", "cpython@3.13"]\n'
        ),
    )
    file_editor = InMemoryFileEditor()
    merged_config = {
        "dependency-groups": {
            "testing@cpython-3.12": ["pytest"],
            "testing@cpython-3.13": ["pytest"],
        },
        "tool": {
            "finecode": {
                "env": {
                    "testing@cpython-3.12": {"interpreter": "cpython@3.12"},
                    "testing@cpython-3.13": {"interpreter": "cpython@3.13"},
                }
            }
        },
    }

    async with handler_test_session(
        project_dir=tmp_path,
        actions=_actions(envs=["testing"]),
        service_overrides={
            IFileEditor: file_editor,
            IProjectInfoProvider: _StubProjectInfoProvider(tmp_path, merged_config),
        },
    ) as session:
        result = await session.run_action(
            _ACTION_NAME, SyncPythonInterpretersRunPayload(save=True)
        )

    assert result.saved is False
    assert file_editor.writes == []
    assert [axis.changed for axis in result.axes] == [False]


def _preset_axis_merged_config(*interpreters: str) -> dict:
    """Merged config as a handler really receives it when a preset pins the axis.

    Config resolution expands a matrix env into one concrete child per interpreter
    before any handler sees the config (read_configs.py, ADR-0047), so a preset-pinned
    axis never arrives as an `interpreters` list -- it arrives already expanded, and the
    base env name is gone.
    """
    return {
        "dependency-groups": {
            f"testing@{value.replace('@', '-')}": ["pytest"] for value in interpreters
        },
        "tool": {
            "finecode": {
                "env": {
                    f"testing@{value.replace('@', '-')}": {"interpreter": value}
                    for value in interpreters
                }
            }
        },
    }


async def test_axis_from_another_config_layer_is_materialized_and_reported(
    tmp_path: pathlib.Path,
) -> None:
    # A preset may pin an env's axis. Derivation writes into the project's own file,
    # which wins that merge -- the intended precedence, and the only way to derive over a
    # pin, since a config key can be replaced but never unset. The pin going dead is
    # worth one warning, though, or nothing marks that it stopped having any effect.
    _write_pyproject(tmp_path, requires_python=">=3.12,<3.14")
    file_editor = InMemoryFileEditor()
    logger = CollectingLogger()

    async with handler_test_session(
        project_dir=tmp_path,
        actions=_actions(envs=["testing"]),
        service_overrides={
            IFileEditor: file_editor,
            ILogger: logger,
            IProjectInfoProvider: _StubProjectInfoProvider(
                tmp_path, _preset_axis_merged_config("cpython@3.11")
            ),
        },
    ) as session:
        result = await session.run_action(
            _ACTION_NAME, SyncPythonInterpretersRunPayload(save=True)
        )

    assert result.saved is True
    written = tomlkit.parse(file_editor.contents(tmp_path / "pyproject.toml"))
    assert written["tool"]["finecode"]["env"]["testing"]["interpreters"] == [
        "cpython@3.12",
        "cpython@3.13",
    ]
    assert len(logger.warnings) == 1
    assert "testing" in logger.warnings[0]


async def test_axis_from_another_config_layer_is_reported_without_saving(
    tmp_path: pathlib.Path,
) -> None:
    # check_toolchains runs the sync with save=False, so the report has to reach CI
    # without a write -- that is where a shadowed pin is most likely to be noticed.
    _write_pyproject(tmp_path, requires_python=">=3.12,<3.14")
    file_editor = InMemoryFileEditor()
    logger = CollectingLogger()

    async with handler_test_session(
        project_dir=tmp_path,
        actions=_actions(envs=["testing"]),
        service_overrides={
            IFileEditor: file_editor,
            ILogger: logger,
            IProjectInfoProvider: _StubProjectInfoProvider(
                tmp_path, _preset_axis_merged_config("cpython@3.11")
            ),
        },
    ) as session:
        await session.run_action(
            _ACTION_NAME, SyncPythonInterpretersRunPayload(save=False)
        )

    assert file_editor.writes == []
    assert len(logger.warnings) == 1


async def test_axis_already_owned_by_this_file_is_not_reported(
    tmp_path: pathlib.Path,
) -> None:
    # The steady state after materialization: children exist in the merged config
    # because this file's own axis produced them. Warning here would fire on every run
    # of every matrix project.
    _write_pyproject(
        tmp_path,
        requires_python=">=3.12,<3.14",
        env_table=(
            "\n[tool.finecode.env.testing]\n"
            'interpreters = ["cpython@3.12", "cpython@3.13"]\n'
        ),
    )
    logger = CollectingLogger()

    async with handler_test_session(
        project_dir=tmp_path,
        actions=_actions(envs=["testing"]),
        service_overrides={
            IFileEditor: InMemoryFileEditor(),
            ILogger: logger,
            IProjectInfoProvider: _StubProjectInfoProvider(
                tmp_path, _preset_axis_merged_config("cpython@3.12", "cpython@3.13")
            ),
        },
    ) as session:
        result = await session.run_action(
            _ACTION_NAME, SyncPythonInterpretersRunPayload(save=True)
        )

    assert result.saved is False
    assert logger.warnings == []


async def test_missing_requires_python_is_an_error(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "sample"\n\n[dependency-groups]\ndev = ["pytest"]\n'
    )

    async with handler_test_session(
        project_dir=tmp_path,
        actions=_actions(envs=["dev"]),
        service_overrides={IFileEditor: InMemoryFileEditor()},
    ) as session:
        with pytest.raises(ActionRunFailed, match="requires-python not found"):
            await session.run_action(
                _ACTION_NAME, SyncPythonInterpretersRunPayload(save=True)
            )


async def test_write_preserves_unrelated_content(tmp_path: pathlib.Path) -> None:
    project_def_path = tmp_path / "pyproject.toml"
    project_def_path.write_text(
        "# keep me\n"
        "[project]\n"
        'name = "sample"\n'
        'requires-python = ">=3.13,<3.14"\n'
        "\n"
        "[dependency-groups]\n"
        'dev = ["pytest"]\n'
    )
    file_editor = InMemoryFileEditor()

    async with handler_test_session(
        project_dir=tmp_path,
        actions=_actions(envs=["dev"]),
        service_overrides={IFileEditor: file_editor},
    ) as session:
        await session.run_action(
            _ACTION_NAME, SyncPythonInterpretersRunPayload(save=True)
        )

    written = file_editor.contents(project_def_path)
    assert "# keep me" in written
    assert 'name = "sample"' in written
