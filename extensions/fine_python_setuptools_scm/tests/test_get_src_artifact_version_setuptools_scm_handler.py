import asyncio
import pathlib
import tomllib
import typing

from fine_src_artifacts import get_src_artifact_version_action
from finecode_extension_api.resource_uri import path_to_resource_uri

from fine_python_setuptools_scm.get_src_artifact_version_setuptools_scm_handler import (
    GetSrcArtifactVersionSetuptoolsScmHandler,
    GetSrcArtifactVersionSetuptoolsScmHandlerConfig,
)

PYPROJECT_TEMPLATE = """\
[project]
name = "pkg"
dynamic = ["version"]

[tool.setuptools_scm]
version_file = "src/pkg/_version.py"
"""


class _FakeLogger:
    def debug(self, message: str) -> None: ...
    def warning(self, message: str) -> None: ...


class _FakeProjectInfoProvider:
    def __init__(self, def_path: pathlib.Path) -> None:
        self._def_path = def_path

    def get_current_project_def_path(self) -> pathlib.Path:
        return self._def_path

    async def get_project_raw_config(
        self, project_def_path: pathlib.Path
    ) -> dict[str, typing.Any]:
        text = await asyncio.to_thread(project_def_path.read_text, encoding="utf-8")
        return tomllib.loads(text)


def _write_fixture(tmp_path: pathlib.Path) -> pathlib.Path:
    def_path = tmp_path / "pyproject.toml"
    def_path.write_text(PYPROJECT_TEMPLATE, encoding="utf-8")
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    return def_path


def _make_handler(def_path: pathlib.Path) -> GetSrcArtifactVersionSetuptoolsScmHandler:
    return GetSrcArtifactVersionSetuptoolsScmHandler(
        config=GetSrcArtifactVersionSetuptoolsScmHandlerConfig(),
        project_info_provider=typing.cast(
            typing.Any, _FakeProjectInfoProvider(def_path)
        ),
        logger=typing.cast(typing.Any, _FakeLogger()),
    )


def _payload(
    def_path: pathlib.Path | None,
) -> get_src_artifact_version_action.GetSrcArtifactVersionRunPayload:
    if def_path is None:
        return get_src_artifact_version_action.GetSrcArtifactVersionRunPayload(
            src_artifact_def_path=None
        )
    return get_src_artifact_version_action.GetSrcArtifactVersionRunPayload(
        src_artifact_def_path=path_to_resource_uri(def_path)
    )


async def test_resource_uri_def_path_returns_version(
    tmp_path: pathlib.Path, monkeypatch: typing.Any
) -> None:
    monkeypatch.setenv("SETUPTOOLS_SCM_PRETEND_VERSION", "1.2.3.dev4")
    def_path = _write_fixture(tmp_path)

    result = await _make_handler(def_path).run(
        _payload(def_path), typing.cast(typing.Any, None)
    )

    assert result.version == "1.2.3.dev4"


async def test_none_def_path_falls_back_to_current_project(
    tmp_path: pathlib.Path, monkeypatch: typing.Any
) -> None:
    monkeypatch.setenv("SETUPTOOLS_SCM_PRETEND_VERSION", "1.2.3.dev4")
    def_path = _write_fixture(tmp_path)

    result = await _make_handler(def_path).run(
        _payload(None), typing.cast(typing.Any, None)
    )

    assert result.version == "1.2.3.dev4"


async def test_scm_handler_alone_leaves_single_quoted_file(
    tmp_path: pathlib.Path, monkeypatch: typing.Any
) -> None:
    monkeypatch.setenv("SETUPTOOLS_SCM_PRETEND_VERSION", "1.2.3.dev4")
    def_path = _write_fixture(tmp_path)

    result = await _make_handler(def_path).run(
        _payload(def_path), typing.cast(typing.Any, None)
    )

    assert result.version == "1.2.3.dev4"
    content = (tmp_path / "src" / "pkg" / "_version.py").read_text(encoding="utf-8")
    assert "__version__ = version = '1.2.3.dev4'" in content
