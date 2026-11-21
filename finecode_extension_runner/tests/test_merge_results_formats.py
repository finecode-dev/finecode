from __future__ import annotations

import dataclasses
import typing
from pathlib import Path

import pytest
from finecode_extension_api import code_action, textstyler
from finecode_extension_api.code_action import CoverageStatus, ItemCoverage
from finecode_extension_api.resource_uri import ResourceUri

from finecode_extension_runner import er_server
from finecode_extension_runner.testing import handler_test_session


@dataclasses.dataclass
class _Result(code_action.RunActionResult):
    text: str = ""

    def update(self, other: code_action.RunActionResult) -> None:
        if isinstance(other, _Result):
            self.text += other.text

    def to_text(self) -> str | textstyler.StyledText:
        return self.text


class _Action(
    code_action.Action[
        code_action.RunActionPayload,
        code_action.RunActionContext[code_action.RunActionPayload],
        _Result,
    ]
):
    PAYLOAD_TYPE = code_action.RunActionPayload
    RUN_CONTEXT_TYPE = code_action.RunActionContext
    RESULT_TYPE = _Result


_ACTION_NAME = _Action.__name__
_ACTION_SOURCE = f"{_Action.__module__}.{_Action.__qualname__}"
_ACTIONS: dict[str, typing.Any] = {
    _ACTION_NAME: {"source": _ACTION_SOURCE, "handlers": []}
}


async def _merge(
    tmp_path: Path,
    results: list[dict[str, typing.Any]],
    formats: list[typing.Literal["json", "string"]] | None,
) -> dict[str, typing.Any]:
    async with handler_test_session(project_dir=tmp_path, actions=_ACTIONS) as session:
        server = er_server.ErServer()
        server._runner_context = session._runner_context
        params: dict[str, typing.Any] = {
            "actionName": _ACTION_NAME,
            "results": results,
        }
        if formats is not None:
            params["resultFormats"] = formats
        return await er_server.merge_results_cmd(server, params)


async def test_merge_results_defaults_to_asdict_json(tmp_path: Path) -> None:
    expected = dataclasses.asdict(_Result(text="final"))
    assert await _merge(tmp_path, [expected], None) == {
        "resultByFormat": {"json": expected}
    }


async def test_merge_results_returns_requested_string(tmp_path: Path) -> None:
    first = dataclasses.asdict(_Result(text="first"))
    second = dataclasses.asdict(_Result(text=" last"))
    assert await _merge(tmp_path, [first, second], ["string"]) == {
        "resultByFormat": {"string": "first last"}
    }


async def test_merge_results_returns_json_and_string(tmp_path: Path) -> None:
    expected = dataclasses.asdict(_Result(text="final"))
    assert await _merge(tmp_path, [expected], ["json", "string"]) == {
        "resultByFormat": {"json": expected, "string": "final"}
    }


async def test_merge_results_includes_unhandled_coverage_block(tmp_path: Path) -> None:
    result = _Result(
        text="body",
        coverage=[
            ItemCoverage(
                status=CoverageStatus.NO_SUBACTIONS,
                item=ResourceUri("file:///input.py"),
            )
        ],
    )
    merged = await _merge(tmp_path, [dataclasses.asdict(result)], ["string"])
    assert "unhandled:" in merged["resultByFormat"]["string"]
    assert "file:///input.py" in merged["resultByFormat"]["string"]


async def test_merge_results_returns_styled_text_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def styled_text(self: _Result) -> textstyler.StyledText:
        result = textstyler.StyledText()
        result.append("styled body")
        return result

    monkeypatch.setattr(_Result, "to_text", styled_text)
    merged = await _merge(
        tmp_path, [dataclasses.asdict(_Result(text="body"))], ["string"]
    )
    assert "styled_text_json" in merged["resultByFormat"]
    assert "string" not in merged["resultByFormat"]


async def test_merge_results_empty_has_empty_formats(tmp_path: Path) -> None:
    assert await _merge(tmp_path, [], ["json", "string"]) == {"resultByFormat": {}}


async def test_merge_results_render_exception_is_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_render(self: _Result) -> str:
        raise ValueError("cannot render")

    monkeypatch.setattr(_Result, "to_text", raise_render)
    result = await _merge(tmp_path, [dataclasses.asdict(_Result(text="x"))], ["string"])
    assert result == {"error": "cannot render"}
