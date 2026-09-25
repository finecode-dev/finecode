"""The semantic-tokens dispatcher answers "no provider for this document".

An empty partial is a no-op for the LSP delta endpoint: tokens are absolute
positions and delta encoding is endpoint-side, so an empty token list simply
concatenates to nothing (R-306).
"""

from __future__ import annotations

import pathlib
import typing

from fine_src_artifacts import group_src_artifact_files_by_lang_action
from finecode_extension_api import code_action
from finecode_extension_api.code_action import CoverageStatus, ItemCoverage
from finecode_extension_api.interfaces import iprojectactionrunner
from finecode_extension_api.resource_uri import path_to_resource_uri

from fine_semantic_tokens import text_document_semantic_tokens_action
from fine_semantic_tokens.semantic_tokens_dispatch_handler import (
    SemanticTokensDispatchHandler,
)

_URI = path_to_resource_uri(pathlib.Path("/tmp/doc.rs"))
_PAYLOAD = text_document_semantic_tokens_action.SemanticTokensPayload(uri=_URI)


class _FakeLogger:
    def debug(self, message: str) -> None: ...


class _CollectingSender:
    def __init__(self) -> None:
        self.results: list[code_action.RunActionResult] = []

    async def send(self, result: code_action.RunActionResult) -> None:
        self.results.append(result)


class _RunContextStub:
    def __init__(self, sender: _CollectingSender) -> None:
        self.meta = code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.USER, dev_env=code_action.DevEnv.CLI
        )
        self.partial_result_sender = sender  # type: ignore[assignment]


class _FakeActionRunner:
    def __init__(
        self,
        subactions_by_lang: dict[str, iprojectactionrunner.ActionRef],
        files_by_lang: dict[str, list],
        subaction_partials: list[
            text_document_semantic_tokens_action.SemanticTokensResult
        ]
        | None = None,
    ) -> None:
        self._subactions = subactions_by_lang
        self._files_by_lang = files_by_lang
        self._subaction_partials = subaction_partials or []

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
        return group_src_artifact_files_by_lang_action.GroupSrcArtifactFilesByLangRunResult(
            files_by_lang=self._files_by_lang
        )

    async def run_action_iter(
        self,
        action_type: iprojectactionrunner.ActionRef,
        payload: typing.Any,
        meta: code_action.RunActionMeta,
        caller_kwargs: code_action.CallerRunContextKwargs | None = None,
    ) -> typing.Any:
        for partial in self._subaction_partials:
            yield partial


async def _run(
    subactions_by_lang: dict[str, iprojectactionrunner.ActionRef],
    files_by_lang: dict[str, list],
    subaction_partials: list[text_document_semantic_tokens_action.SemanticTokensResult]
    | None = None,
) -> list[code_action.RunActionResult]:
    handler = SemanticTokensDispatchHandler(
        action_runner=typing.cast(
            iprojectactionrunner.IProjectActionRunner,
            _FakeActionRunner(subactions_by_lang, files_by_lang, subaction_partials),
        ),
        logger=typing.cast(typing.Any, _FakeLogger()),
    )
    sender = _CollectingSender()
    await handler.run(
        payload=_PAYLOAD,
        run_context=typing.cast(typing.Any, _RunContextStub(sender)),
    )
    return sender.results


async def test_uncovered_language_sends_one_empty_partial_naming_the_document() -> None:
    results = await _run(
        subactions_by_lang={"python": typing.cast(typing.Any, object())},
        files_by_lang={"rust": [_URI]},
    )
    assert len(results) == 1
    result = results[0]
    assert isinstance(result, text_document_semantic_tokens_action.SemanticTokensResult)
    assert result.tokens == []
    assert result.unhandled == [
        ItemCoverage(
            status=CoverageStatus.NO_SUBACTION_FOR_LANGUAGE,
            item=_URI,
            detail="rust",
        )
    ]


async def test_no_subactions_sends_a_no_subactions_partial() -> None:
    results = await _run(subactions_by_lang={}, files_by_lang={})
    assert len(results) == 1
    assert results[0].tokens == []
    assert results[0].unhandled == [
        ItemCoverage(status=CoverageStatus.NO_SUBACTIONS, item=_URI)
    ]


async def test_covered_document_sends_no_coverage_only_partial() -> None:
    results = await _run(
        subactions_by_lang={"python": typing.cast(typing.Any, object())},
        files_by_lang={"python": [_URI]},
        subaction_partials=[
            text_document_semantic_tokens_action.SemanticTokensResult(coverage=[])
        ],
    )
    assert len(results) == 1
    assert results[0].tokens == []
    assert results[0].unhandled == []
