from __future__ import annotations

import pathlib

import pytest
from fine_dist_artifacts.get_dist_artifact_version_action import (
    GetDistArtifactVersionAction,
    GetDistArtifactVersionRunPayload,
    GetDistArtifactVersionRunResult,
)
from finecode_extension_api.resource_uri import ResourceUri, path_to_resource_uri
from finecode_extension_runner.testing import run_handler

from fine_python_package_info.get_dist_artifact_version_py_handler import (
    GetDistArtifactVersionPyHandler,
)


@pytest.mark.parametrize(
    "filename",
    [
        "example-2.3.4-py3-none-any.whl",
        "example-2.3.4.tar.gz",
        "example-2.3.4.zip",
    ],
)
@pytest.mark.parametrize("encoded_version", [False, True])
async def test_distribution_uri_returns_filename_version(
    tmp_path: pathlib.Path, filename: str, encoded_version: bool
) -> None:
    uri = path_to_resource_uri(tmp_path / "dist café%20" / filename)
    if encoded_version:
        uri = ResourceUri(uri.replace("2.3.4", "%32.3.4"))

    result = await run_handler(
        GetDistArtifactVersionPyHandler,
        GetDistArtifactVersionRunPayload(dist_artifact_path=uri),
        action_cls=GetDistArtifactVersionAction,
        project_dir=tmp_path,
    )

    assert isinstance(result, GetDistArtifactVersionRunResult)
    assert result.version == "2.3.4"
