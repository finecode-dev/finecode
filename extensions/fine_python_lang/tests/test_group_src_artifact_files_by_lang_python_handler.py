"""The python grouping handler treats ``langs`` as advisory, which coverage relies on.

Same contract as the toml twin: the lint/type-check dispatch passes only the
registered subaction languages to the grouping action, and derives per-file
coverage from the returned buckets. The python handler only ever produces a
``python`` bucket but must not stop producing it when the filter excludes
python — otherwise a python file would be misclassified as an
undetectable-language miss.
"""

from __future__ import annotations

import pathlib
from pathlib import Path

from fine_src_artifacts.group_src_artifact_files_by_lang_action import (
    GroupSrcArtifactFilesByLangAction,
    GroupSrcArtifactFilesByLangRunPayload,
)
from finecode_extension_api.resource_uri import path_to_resource_uri
from finecode_extension_runner.testing import run_handler

from fine_python_lang.group_src_artifact_files_by_lang_python_handler import (
    GroupSrcArtifactFilesByLangPythonHandler,
)
from fine_python_lang.ipypackagelayoutinfoprovider import (
    IPyPackageLayoutInfoProvider,
    PyPackageLayout,
)


class _SrcLayoutStub:
    def __init__(self, src_root: pathlib.Path) -> None:
        self._src_root = src_root

    async def get_package_layout(
        self, package_dir_path: pathlib.Path
    ) -> PyPackageLayout:
        return PyPackageLayout.SRC

    async def get_package_src_root_dir_path(
        self, package_dir_path: str
    ) -> pathlib.Path:
        return self._src_root


async def test_langs_filter_is_advisory_for_the_python_handler(
    tmp_path: Path,
) -> None:
    """A ``.py`` file requested with ``langs`` that excludes ``python`` still
    lands in the ``python`` bucket — the filter narrows nothing, and
    coverage attribution depends on that."""
    src_root = tmp_path / "src"
    src_root.mkdir(parents=True)
    py_uri = path_to_resource_uri(src_root / "mod.py")
    (src_root / "mod.py").touch()
    toml_uri = path_to_resource_uri(tmp_path / "config.toml")

    result = await run_handler(
        GroupSrcArtifactFilesByLangPythonHandler,
        GroupSrcArtifactFilesByLangRunPayload(
            file_paths=[py_uri, toml_uri], langs=["toml"]
        ),
        action_cls=GroupSrcArtifactFilesByLangAction,
        project_dir=pathlib.Path(tmp_path),
        service_overrides={
            IPyPackageLayoutInfoProvider: _SrcLayoutStub(src_root)  # type: ignore[arg-type]
        },
    )
    assert result is not None
    assert result.files_by_lang == {"python": [py_uri]}