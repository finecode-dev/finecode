from __future__ import annotations

import pathlib

from fine_src_artifacts.get_src_artifact_toolchain_range_action import (
    GetSrcArtifactToolchainRangeAction,
    GetSrcArtifactToolchainRangeRunPayload,
    GetSrcArtifactToolchainRangeRunResult,
)
from finecode_extension_api.interfaces.ilogger import ILogger
from finecode_extension_runner.testing import handler_test_session

from fine_python_package_info.get_src_artifact_toolchain_range_py_handler import (
    GetSrcArtifactToolchainRangePyHandler,
)
from tests.stubs import CollectingLogger

_ACTION_NAME = GetSrcArtifactToolchainRangeAction.__name__
_ACTION_SOURCE = (
    f"{GetSrcArtifactToolchainRangeAction.__module__}"
    f".{GetSrcArtifactToolchainRangeAction.__qualname__}"
)
_HANDLER_NAME = GetSrcArtifactToolchainRangePyHandler.__name__
_HANDLER_SOURCE = (
    f"{GetSrcArtifactToolchainRangePyHandler.__module__}"
    f".{GetSrcArtifactToolchainRangePyHandler.__qualname__}"
)
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
        }
    }


def _write_pyproject(
    project_dir: pathlib.Path, requires_python: str | None = ">=3.11"
) -> pathlib.Path:
    project_def_path = project_dir / "pyproject.toml"
    requires_python_line = (
        f'requires-python = "{requires_python}"\n'
        if requires_python is not None
        else ""
    )
    project_def_path.write_text(
        "[project]\n"
        'name = "sample"\n' + requires_python_line + "\n"
        "[dependency-groups]\n"
        'dev = ["pytest"]\n'
    )
    return project_def_path


async def test_open_range_is_derived_from_requires_python(
    tmp_path: pathlib.Path,
) -> None:
    _write_pyproject(tmp_path, requires_python=">=3.11")

    async with handler_test_session(
        project_dir=tmp_path, actions=_actions()
    ) as session:
        result = await session.run_action(
            _ACTION_NAME, GetSrcArtifactToolchainRangeRunPayload()
        )

    assert (result.min_version, result.max_version) == ("3.11", None)
    assert result.derived_from is not None
    assert "requires-python" in result.derived_from


async def test_bounded_range_is_derived_from_requires_python(
    tmp_path: pathlib.Path,
) -> None:
    _write_pyproject(tmp_path, requires_python=">=3.11,<3.14")

    async with handler_test_session(
        project_dir=tmp_path, actions=_actions()
    ) as session:
        result = await session.run_action(
            _ACTION_NAME, GetSrcArtifactToolchainRangeRunPayload()
        )

    assert (result.min_version, result.max_version) == ("3.11", "3.13")


async def test_missing_requires_python_reports_nothing_and_warns(
    tmp_path: pathlib.Path,
) -> None:
    # nothing declared is not a failure: the consumers of this range are linters and
    # formatters, and a project without requires-python must still be lintable. It is
    # diagnosable, though, so it does not pass silently.
    _write_pyproject(tmp_path, requires_python=None)
    logger = CollectingLogger()

    async with handler_test_session(
        project_dir=tmp_path,
        actions=_actions(),
        service_overrides={ILogger: logger},
    ) as session:
        result = await session.run_action(
            _ACTION_NAME, GetSrcArtifactToolchainRangeRunPayload()
        )

    assert (result.min_version, result.max_version, result.derived_from) == (
        None,
        None,
        None,
    )
    assert any("requires-python" in warning for warning in logger.warnings)


async def test_pinned_config_overrides_the_declaration(tmp_path: pathlib.Path) -> None:
    _write_pyproject(tmp_path, requires_python=">=3.9")

    async with handler_test_session(
        project_dir=tmp_path, actions=_actions(min_version="3.12")
    ) as session:
        result = await session.run_action(
            _ACTION_NAME, GetSrcArtifactToolchainRangeRunPayload()
        )

    assert result.min_version == "3.12"
    assert result.derived_from is not None
    # the pin and the declaration it shadows are both named, so a surprising target
    # version can be traced to the layer that decided it
    assert "pinned in handler config" in result.derived_from
    assert "requires-python" in result.derived_from


async def test_both_ends_pinned_ignores_a_missing_declaration(
    tmp_path: pathlib.Path,
) -> None:
    _write_pyproject(tmp_path, requires_python=None)
    logger = CollectingLogger()

    async with handler_test_session(
        project_dir=tmp_path,
        actions=_actions(min_version="3.11", max_version="3.13"),
        service_overrides={ILogger: logger},
    ) as session:
        result = await session.run_action(
            _ACTION_NAME, GetSrcArtifactToolchainRangeRunPayload()
        )

    assert (result.min_version, result.max_version) == ("3.11", "3.13")
    assert logger.warnings == []


def test_contributions_intersect_rather_than_replace() -> None:
    # merging may narrow a declared range but never widen one, so registering a second
    # handler can add a constraint the first did not know about, never drop one it made
    result = GetSrcArtifactToolchainRangeRunResult(min_version="3.9", max_version=None)
    result.update(
        GetSrcArtifactToolchainRangeRunResult(min_version="3.11", max_version="3.13")
    )

    assert (result.min_version, result.max_version) == ("3.11", "3.13")


def test_intersection_keeps_the_narrower_end_regardless_of_order() -> None:
    result = GetSrcArtifactToolchainRangeRunResult(
        min_version="3.11", max_version="3.12"
    )
    result.update(
        GetSrcArtifactToolchainRangeRunResult(min_version="3.9", max_version="3.13")
    )

    assert (result.min_version, result.max_version) == ("3.11", "3.12")
