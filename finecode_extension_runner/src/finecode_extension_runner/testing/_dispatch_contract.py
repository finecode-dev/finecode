"""Shared contract test for language-fan-out dispatch handlers (R-307 / R-308).

A "dispatch handler" (e.g. ``LintFilesDispatchHandler``, ``TypeCheckFilesDispatchHandler``)
groups a flat list of files by language via a grouping action and fans out
per-language work to subactions registered through ``PARENT_ACTION``/``LANGUAGE``.

R-307 (docs/guides/designing-actions-rules.md) requires every file in the
payload to be covered by a result. That includes files grouped into a
language for which *no subaction is registered* under the action being
dispatched — for example, ``toml`` files reaching a ``type_check_files``
dispatch handler that only has a ``python`` subaction, because there is no
``type_check_toml_files`` equivalent of ``lint_toml_files``. A dispatch
handler that simply skips such files can end up sending nothing at all (R-308
violation), which crashes not just that one project's call but, because
top-level handlers like ``TypeCheckHandler`` await all projects in a single
``asyncio.TaskGroup``, the entire workspace-wide call.

A grouping handler is free to ignore the ``langs`` filter on its own payload
(the real ``fine_toml_lang`` handler does — it always reports every file of
its language) and that is exactly the scenario this test reproduces: a file
is grouped into a real language bucket, but that bucket has no registered
subaction for the action under test.

Usage::

    from finecode_extension_runner.testing import LanguageDispatchCoverageTests
    from fine_src_artifacts.group_src_artifact_files_by_lang_action import (
        GroupSrcArtifactFilesByLangAction,
    )

    class TestTypeCheckFilesDispatchHandlerCoverage(LanguageDispatchCoverageTests):
        dispatch_handler_cls = TypeCheckFilesDispatchHandler
        parent_action_cls = TypeCheckFilesAction
        group_action_cls = GroupSrcArtifactFilesByLangAction

``group_action_cls`` is caller-supplied rather than imported here so that this
generic harness (part of ``finecode_extension_runner``, the runtime every
extension depends on) does not itself depend on a feature preset like
``fine_src_artifacts``.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any, ClassVar

from finecode_extension_api import code_action
from finecode_extension_api.resource_uri import (
    path_to_resource_uri,
    resource_uri_to_path,
)

_COVERED_LANG = "covered_lang"
_UNCOVERED_LANG = "uncovered_lang"


def _make_group_handler_run(group_result_cls: type) -> Any:
    async def run(self: Any, payload: Any, run_context: Any) -> Any:
        # Mirrors real-world grouping handlers (e.g. the TOML one), which
        # classify by file extension and ignore payload.langs entirely.
        files_by_lang: dict[str, list] = {}
        for uri in payload.file_paths:
            suffix = resource_uri_to_path(uri).suffix
            if suffix == ".covered":
                files_by_lang.setdefault(_COVERED_LANG, []).append(uri)
            elif suffix == ".uncovered":
                files_by_lang.setdefault(_UNCOVERED_LANG, []).append(uri)
        return group_result_cls(files_by_lang=files_by_lang)

    return run


def _make_covered_handler_run(result_cls: type) -> Any:
    async def run(self: Any, payload: Any, run_context: Any) -> Any:
        return result_cls(messages={uri: [] for uri in payload.file_paths})

    return run


class LanguageDispatchCoverageTests:
    """Pytest base class verifying R-307/R-308 compliance for a
    group-by-language dispatch handler.

    Subclasses must set:

    ``dispatch_handler_cls``
        The dispatch handler under test (e.g. ``TypeCheckFilesDispatchHandler``).
    ``parent_action_cls``
        The action it dispatches for (e.g. ``TypeCheckFilesAction``). Its
        ``PAYLOAD_TYPE`` / ``RUN_CONTEXT_TYPE`` / ``RESULT_TYPE`` are reused
        as-is to build a stub "covered language" subaction, mirroring how real
        language subactions reuse the parent action's types.
    ``group_action_cls``
        The grouping action the dispatch handler calls to bucket files by
        language (e.g. ``GroupSrcArtifactFilesByLangAction``).
    """

    dispatch_handler_cls: ClassVar[type]
    parent_action_cls: ClassVar[type]
    group_action_cls: ClassVar[type]

    async def test_uncovered_language_files_still_get_a_result(
        self, tmp_path: Path
    ) -> None:
        from finecode_extension_runner.testing import handler_test_session

        parent_action_cls = self.parent_action_cls
        group_action_cls = self.group_action_cls
        suffix = uuid.uuid4().hex[:8]
        this_module = sys.modules[__name__]

        # Build a "covered language" subaction + handler that reuse the
        # parent action's own payload/result/context types — exactly how a
        # real language subaction (e.g. lint_python_files) is defined.
        covered_subaction_cls = type(
            f"_CoveredLangSubaction_{suffix}",
            (code_action.Action,),
            {
                "DESCRIPTION": "test stub covered-language subaction",
                "PAYLOAD_TYPE": parent_action_cls.PAYLOAD_TYPE,
                "RUN_CONTEXT_TYPE": parent_action_cls.RUN_CONTEXT_TYPE,
                "RESULT_TYPE": parent_action_cls.RESULT_TYPE,
                "PARENT_ACTION": parent_action_cls,
                "LANGUAGE": _COVERED_LANG,
                "__module__": __name__,
            },
        )
        covered_handler_cls = type(
            f"_CoveredLangHandler_{suffix}",
            (object,),
            {
                "run": _make_covered_handler_run(parent_action_cls.RESULT_TYPE),
                "__module__": __name__,
            },
        )
        group_handler_cls = type(
            f"_GroupHandler_{suffix}",
            (object,),
            {
                "run": _make_group_handler_run(group_action_cls.RESULT_TYPE),
                "__module__": __name__,
            },
        )
        for cls in (covered_subaction_cls, covered_handler_cls, group_handler_cls):
            setattr(this_module, cls.__name__, cls)

        def _source(cls: type) -> str:
            return f"{cls.__module__}.{cls.__qualname__}"

        covered_file = path_to_resource_uri((tmp_path / "a.covered").resolve())
        uncovered_file = path_to_resource_uri((tmp_path / "b.uncovered").resolve())

        actions = {
            "parent_action": {
                "source": _source(parent_action_cls),
                "handlers": [
                    {
                        "name": "dispatch",
                        "source": _source(self.dispatch_handler_cls),
                        "env": "test",
                    }
                ],
            },
            "group_action": {
                "source": _source(group_action_cls),
                "handlers": [
                    {
                        "name": "group",
                        "source": _source(group_handler_cls),
                        "env": "test",
                    }
                ],
            },
            "covered_lang_subaction": {
                "source": _source(covered_subaction_cls),
                "handlers": [
                    {
                        "name": "covered",
                        "source": _source(covered_handler_cls),
                        "env": "test",
                    }
                ],
            },
        }

        try:
            async with handler_test_session(
                project_dir=tmp_path,
                actions=actions,
            ) as session:
                result = await session.run_action(
                    "parent_action",
                    parent_action_cls.PAYLOAD_TYPE(
                        file_paths=[covered_file, uncovered_file]
                    ),
                )
        finally:
            for cls in (covered_subaction_cls, covered_handler_cls, group_handler_cls):
                delattr(this_module, cls.__name__)

        assert result is not None, (
            f"{self.dispatch_handler_cls.__name__} sent no result at all when "
            f"some files were grouped into a language ({_UNCOVERED_LANG!r}) with "
            "no registered subaction. Per R-307/R-308, it must send an empty "
            "result for files it cannot dispatch, not drop them silently."
        )
        assert covered_file in result.messages
        assert uncovered_file in result.messages, (
            f"File grouped into {_UNCOVERED_LANG!r} (no registered subaction) is "
            "missing from the result. R-307 requires every input file to be "
            "covered, even when no subaction can process it."
        )
        assert result.messages[uncovered_file] == []
