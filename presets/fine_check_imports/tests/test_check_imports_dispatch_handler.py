"""check_imports dispatch coverage: an unregistered language is a miss named
with the language.

``messages == {}`` is also the legitimate "no import problems" answer; the
coverage entry naming the artifact definition with the detected language as
detail is what separates the two.
"""

from __future__ import annotations

import pathlib
import typing

from fine_src_artifacts.get_src_artifact_language_action import (
    GetSrcArtifactLanguageRunResult,
)
from finecode_extension_api import code_action
from finecode_extension_api.code_action import CoverageStatus, ItemCoverage
from finecode_extension_api.interfaces import iprojectactionrunner
from finecode_extension_api.resource_uri import path_to_resource_uri

from fine_check_imports.check_imports_action import (
    CheckImportsRunPayload,
    CheckImportsRunResult,
)
from fine_check_imports.check_imports_dispatch_handler import (
    CheckImportsDispatchHandler,
)

_SRC_URI = path_to_resource_uri(pathlib.Path("/tmp/pyproject.toml"))


class _FakeLogger:
    def debug(self, message: str) -> None: ...


class _FakeProjectInfoProvider:
    pass


class _FakeActionRunner:
    def __init__(
        self,
        subactions_by_lang: dict[str, iprojectactionrunner.ActionRef],
        language: str,
    ) -> None:
        self._subactions = subactions_by_lang
        self._language = language

    async def get_actions_for_parent(
        self, parent_action_type: type
    ) -> dict[str, iprojectactionrunner.ActionRef]:
        return self._subactions

    async def run_action(
        self,
        action_type: iprojectactionrunner.ActionRef,
        payload: typing.Any,
        meta: code_action.RunActionMeta,
        caller_kwargs: code_action.CallerRunContextKwargs | None = None,
    ) -> typing.Any:
        return GetSrcArtifactLanguageRunResult(language=self._language)


class _RunContextStub:
    def __init__(self) -> None:
        self.meta = code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.USER, dev_env=code_action.DevEnv.CLI
        )


async def _run(
    subactions_by_lang: dict[str, iprojectactionrunner.ActionRef],
    language: str,
) -> CheckImportsRunResult:
    handler = CheckImportsDispatchHandler(
        action_runner=typing.cast(
            iprojectactionrunner.IProjectActionRunner,
            _FakeActionRunner(subactions_by_lang, language),
        ),
        project_info_provider=typing.cast(typing.Any, _FakeProjectInfoProvider()),
        logger=typing.cast(typing.Any, _FakeLogger()),
    )
    return await handler.run(
        payload=CheckImportsRunPayload(src_artifact_def_path=_SRC_URI),
        run_context=typing.cast(typing.Any, _RunContextStub()),
    )


async def test_unregistered_language_is_a_named_miss() -> None:
    """A language with no registered check subaction must answer
    ``messages == {}`` *and* an unhandled entry naming the artifact definition
    with the language as detail."""
    result = await _run(
        subactions_by_lang={"python": typing.cast(typing.Any, object())},
        language="rust",
    )
    assert result.messages == {}
    assert result.unhandled == [
        ItemCoverage(
            status=CoverageStatus.NO_SUBACTION_FOR_LANGUAGE,
            item=_SRC_URI,
            detail="rust",
        )
    ]
