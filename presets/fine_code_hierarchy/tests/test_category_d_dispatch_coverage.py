"""Category-D dispatchers answer "no provider for this document" (plan 19, 26-27).

The six hierarchy dispatchers mirror the symbol-info ones; four of them take
their document location from ``payload.item.uri`` rather than ``payload.uri``.
Same contract: exactly one empty-domain partial naming the document when
nothing covers it.
"""

from __future__ import annotations

import dataclasses
import pathlib
import typing

import pytest
from fine_src_artifacts import group_src_artifact_files_by_lang_action
from finecode_extension_api import code_action, common_types
from finecode_extension_api.code_action import CoverageStatus, ItemCoverage
from finecode_extension_api.interfaces import iprojectactionrunner
from finecode_extension_api.resource_uri import path_to_resource_uri

from fine_code_hierarchy.call_hierarchy_incoming_calls_action import (
    CallHierarchyIncomingCallsPayload,
    CallHierarchyIncomingCallsResult,
)
from fine_code_hierarchy.call_hierarchy_incoming_calls_dispatch_handler import (
    CallHierarchyIncomingCallsDispatchHandler,
)
from fine_code_hierarchy.call_hierarchy_outgoing_calls_action import (
    CallHierarchyOutgoingCallsPayload,
    CallHierarchyOutgoingCallsResult,
)
from fine_code_hierarchy.call_hierarchy_outgoing_calls_dispatch_handler import (
    CallHierarchyOutgoingCallsDispatchHandler,
)
from fine_code_hierarchy.prepare_call_hierarchy_dispatch_handler import (
    PrepareCallHierarchyDispatchHandler,
)
from fine_code_hierarchy.prepare_type_hierarchy_dispatch_handler import (
    PrepareTypeHierarchyDispatchHandler,
)
from fine_code_hierarchy.text_document_prepare_call_hierarchy_action import (
    CallHierarchyItem,
    PrepareCallHierarchyPayload,
    PrepareCallHierarchyResult,
)
from fine_code_hierarchy.text_document_prepare_type_hierarchy_action import (
    PrepareTypeHierarchyPayload,
    PrepareTypeHierarchyResult,
    TypeHierarchyItem,
)
from fine_code_hierarchy.type_hierarchy_subtypes_action import (
    TypeHierarchySubtypesPayload,
    TypeHierarchySubtypesResult,
)
from fine_code_hierarchy.type_hierarchy_subtypes_dispatch_handler import (
    TypeHierarchySubtypesDispatchHandler,
)
from fine_code_hierarchy.type_hierarchy_supertypes_action import (
    TypeHierarchySupertypesPayload,
    TypeHierarchySupertypesResult,
)
from fine_code_hierarchy.type_hierarchy_supertypes_dispatch_handler import (
    TypeHierarchySupertypesDispatchHandler,
)
from fine_code_hierarchy.types import SymbolKind

_URI = path_to_resource_uri(pathlib.Path("/tmp/entity.rs"))
_POS = common_types.Position(line=0, character=0)
_RANGE = common_types.Range(start=_POS, end=_POS)
_CALL_ITEM = CallHierarchyItem(
    name="f",
    kind=SymbolKind.FUNCTION,
    uri=_URI,
    range=_RANGE,
    selection_range=_RANGE,
)
_TYPE_ITEM = TypeHierarchyItem(
    name="T",
    kind=SymbolKind.CLASS,
    uri=_URI,
    range=_RANGE,
    selection_range=_RANGE,
)

_CASES = [
    pytest.param(
        PrepareCallHierarchyDispatchHandler,
        PrepareCallHierarchyPayload(uri=_URI, position=_POS),
        PrepareCallHierarchyResult,
        _URI,
        id="prepare_call_hierarchy",
    ),
    pytest.param(
        CallHierarchyIncomingCallsDispatchHandler,
        CallHierarchyIncomingCallsPayload(item=_CALL_ITEM),
        CallHierarchyIncomingCallsResult,
        _URI,
        id="call_hierarchy_incoming_calls",
    ),
    pytest.param(
        CallHierarchyOutgoingCallsDispatchHandler,
        CallHierarchyOutgoingCallsPayload(item=_CALL_ITEM),
        CallHierarchyOutgoingCallsResult,
        _URI,
        id="call_hierarchy_outgoing_calls",
    ),
    pytest.param(
        PrepareTypeHierarchyDispatchHandler,
        PrepareTypeHierarchyPayload(uri=_URI, position=_POS),
        PrepareTypeHierarchyResult,
        _URI,
        id="prepare_type_hierarchy",
    ),
    pytest.param(
        TypeHierarchySubtypesDispatchHandler,
        TypeHierarchySubtypesPayload(item=_TYPE_ITEM),
        TypeHierarchySubtypesResult,
        _URI,
        id="type_hierarchy_subtypes",
    ),
    pytest.param(
        TypeHierarchySupertypesDispatchHandler,
        TypeHierarchySupertypesPayload(item=_TYPE_ITEM),
        TypeHierarchySupertypesResult,
        _URI,
        id="type_hierarchy_supertypes",
    ),
]


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
        subaction_partials: list[code_action.RunActionResult] | None = None,
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


def _assert_empty_domain(result: code_action.RunActionResult) -> None:
    for field in dataclasses.fields(result):
        if field.name == "coverage":
            continue
        assert not getattr(result, field.name), f"expected empty {field.name}"


async def _run(
    handler_cls: type,
    payload: code_action.RunActionPayload,
    subactions_by_lang: dict[str, iprojectactionrunner.ActionRef],
    files_by_lang: dict[str, list],
    subaction_partials: list[code_action.RunActionResult] | None = None,
) -> list[code_action.RunActionResult]:
    handler = handler_cls(
        action_runner=typing.cast(
            iprojectactionrunner.IProjectActionRunner,
            _FakeActionRunner(subactions_by_lang, files_by_lang, subaction_partials),
        ),
        logger=typing.cast(typing.Any, _FakeLogger()),
    )
    sender = _CollectingSender()
    await handler.run(
        payload=payload,
        run_context=typing.cast(typing.Any, _RunContextStub(sender)),
    )
    return sender.results


@pytest.mark.parametrize(
    ("handler_cls", "payload", "result_cls", "document_uri"), _CASES
)
async def test_uncovered_language_sends_one_empty_partial_naming_the_document(
    handler_cls: type,
    payload: code_action.RunActionPayload,
    result_cls: type,
    document_uri: object,
) -> None:
    results = await _run(
        handler_cls,
        payload,
        subactions_by_lang={"python": typing.cast(typing.Any, object())},
        files_by_lang={"rust": [_URI]},
    )
    assert len(results) == 1
    result = results[0]
    assert isinstance(result, result_cls)
    _assert_empty_domain(result)
    assert result.unhandled == [
        ItemCoverage(
            status=CoverageStatus.NO_SUBACTION_FOR_LANGUAGE,
            item=document_uri,
            detail="rust",
        )
    ]


@pytest.mark.parametrize(
    ("handler_cls", "payload", "result_cls", "document_uri"), _CASES
)
async def test_no_subactions_sends_a_no_subactions_partial(
    handler_cls: type,
    payload: code_action.RunActionPayload,
    result_cls: type,
    document_uri: object,
) -> None:
    results = await _run(handler_cls, payload, subactions_by_lang={}, files_by_lang={})
    assert len(results) == 1
    result = results[0]
    assert isinstance(result, result_cls)
    _assert_empty_domain(result)
    assert result.unhandled == [
        ItemCoverage(status=CoverageStatus.NO_SUBACTIONS, item=document_uri)
    ]


@pytest.mark.parametrize(
    ("handler_cls", "payload", "result_cls", "document_uri"), _CASES
)
async def test_covered_document_sends_no_coverage_only_partial(
    handler_cls: type,
    payload: code_action.RunActionPayload,
    result_cls: type,
    document_uri: object,
) -> None:
    results = await _run(
        handler_cls,
        payload,
        subactions_by_lang={"python": typing.cast(typing.Any, object())},
        files_by_lang={"python": [_URI]},
        subaction_partials=[result_cls(coverage=[])],
    )
    assert len(results) == 1
    assert results[0].unhandled == []
