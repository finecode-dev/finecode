"""The grouping handler treats ``langs`` as advisory, which coverage relies on.

The lint/type-check dispatch passes ``langs=[...registered subactions...]`` to
the grouping action and then derives per-file coverage from the returned
buckets. If a conformant handler honoured ``langs`` strictly, a ``.toml`` file
would never appear in the ``toml`` bucket when no toml subaction is
registered, and the NO_SUBACTION_FOR_LANGUAGE attribution with
``detail == "toml"`` would become undeliverable.

Removing the advisory behavior changes the contract for every dispatch
handler that derives coverage from the grouping result.
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

from fine_toml_lang.group_src_artifact_files_by_lang_toml_handler import (
    GroupSrcArtifactFilesByLangTomlHandler,
)


async def test_langs_filter_is_advisory_for_the_toml_handler(
    tmp_path: Path,
) -> None:
    """A ``.toml`` file requested with ``langs`` that excludes ``toml`` still
    lands in the ``toml`` bucket — the filter narrows nothing, and
    coverage attribution depends on that."""
    toml_uri = path_to_resource_uri(tmp_path / "config.toml")
    result = await run_handler(
        GroupSrcArtifactFilesByLangTomlHandler,
        GroupSrcArtifactFilesByLangRunPayload(file_paths=[toml_uri], langs=["python"]),
        action_cls=GroupSrcArtifactFilesByLangAction,
        project_dir=pathlib.Path(tmp_path),
    )
    assert result is not None
    assert result.files_by_lang == {"toml": [toml_uri]}
