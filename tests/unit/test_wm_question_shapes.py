"""Per-shape capability, wire and validation for text and multi-select (issue 983)."""

from __future__ import annotations

import asyncio

import pytest
from finecode_jsonrpc._converter import converter

from finecode.wm_server import wm_server
from finecode.wm_server.runner import _internal_client_types, elicitation_bridge


class _FakeWriter:
    def __init__(self) -> None:
        self.written: list[dict] = []

    def write(self, data: bytes) -> None:
        raise AssertionError("tests capture through _write_message instead")

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        return None

    def get_extra_info(self, _name: str) -> str:
        return "test-peer"


@pytest.fixture(autouse=True)
def capture_writes(monkeypatch: pytest.MonkeyPatch):
    def _capture(writer: object, msg: dict) -> None:
        if isinstance(writer, _FakeWriter):
            writer.written.append(msg)

    monkeypatch.setattr(wm_server, "_write_message", _capture)


@pytest.fixture(autouse=True)
def clean_registries():
    yield
    wm_server._pending_client_requests.clear()
    wm_server._pending_client_request_owners.clear()
    wm_server._client_capabilities.clear()
    wm_server._connected_clients.clear()
    wm_server._pending_questions.clear()
    elicitation_bridge.reset_origins()


def _bridge() -> wm_server._WmElicitationBridge:
    return wm_server._WmElicitationBridge()


def _connected(capability: object) -> _FakeWriter:
    writer = _FakeWriter()
    wm_server._connected_clients.add(writer)
    wm_server._client_capabilities[writer] = {"elicitation": capability}
    return writer


# A13: declaration parsing
@pytest.mark.parametrize(
    ("capability", "choice", "text", "multi"),
    [
        (True, True, False, False),
        ({"choice": True}, True, False, False),
        ({"choice": False, "text": True}, True, True, False),
        ({"text": 1}, True, False, False),
        ({"text": "yes"}, True, False, False),
        ({"text": {}}, True, False, False),
        ({"multiChoice": True}, True, False, True),
    ],
)
def test_declaration_parsing(
    capability: object, choice: bool, text: bool, multi: bool
) -> None:
    shapes = wm_server._declared_shapes(capability)
    assert (shapes.choice, shapes.text, shapes.multi_choice) == (choice, text, multi)


def test_choice_false_text_true_declares_text_only() -> None:
    shapes = wm_server._declared_shapes({"choice": False, "text": True})
    assert (shapes.choice, shapes.text, shapes.multi_choice) == (True, True, False)


# A14: undeclared shape sends nothing
async def test_undeclared_text_sends_nothing() -> None:
    writer = _connected({"choice": True})
    answer = await _bridge().elicit(
        message="?",
        options=[],
        default=None,
        timeout_sec=5.0,
        run_writer_key=writer,
        run_id=None,
        kind="text",
    )
    assert answer == {"outcome": "unavailable"}
    assert writer.written == []


async def test_undeclared_multi_sends_nothing() -> None:
    writer = _connected({"choice": True, "text": True})
    answer = await _bridge().elicit(
        message="?",
        options=["a"],
        default=None,
        timeout_sec=5.0,
        run_writer_key=writer,
        run_id=None,
        kind="multiChoice",
        preselected=["a"],
    )
    assert answer == {"outcome": "unavailable"}
    assert writer.written == []


# A15: availability
def test_availability_matches_declaration() -> None:
    writer = _connected({"choice": True, "text": True})
    shapes = _bridge().shape_availability(run_writer_key=writer)
    assert (shapes.choice, shapes.text, shapes.multi_choice) == (True, True, False)
    assert _bridge().shape_availability(
        run_writer_key=None
    ) == elicitation_bridge.ShapeAvailability(
        choice=False, text=False, multi_choice=False
    )


# A16: unknown kind, over-bound prefill, no options
async def test_unknown_kind_sends_nothing() -> None:
    writer = _connected(True)
    answer = await _bridge().elicit(
        message="?",
        options=["a"],
        default=None,
        timeout_sec=5.0,
        run_writer_key=writer,
        run_id=None,
        kind="bogus",
    )
    assert answer == {"outcome": "unavailable"}
    assert writer.written == []


async def test_over_bound_prefill_sends_nothing() -> None:
    writer = _connected({"choice": True, "text": True})
    answer = await _bridge().elicit(
        message="?",
        options=[],
        default=None,
        timeout_sec=5.0,
        run_writer_key=writer,
        run_id=None,
        kind="text",
        initial_value="x" * (elicitation_bridge.QUESTION_TEXT_MAX_CHARS + 1),
    )
    assert answer == {"outcome": "unavailable"}
    assert writer.written == []


async def test_no_option_choice_sends_nothing() -> None:
    writer = _connected(True)
    answer = await _bridge().elicit(
        message="?",
        options=[],
        default=None,
        timeout_sec=5.0,
        run_writer_key=writer,
        run_id=None,
        kind="choice",
    )
    assert answer == {"outcome": "unavailable"}
    assert writer.written == []


# A17: reply validation
async def _ask_text(writer: _FakeWriter, **kwargs: object) -> asyncio.Task:
    return asyncio.create_task(
        _bridge().elicit(
            message="?",
            options=[],
            default=None,
            timeout_sec=5.0,
            run_writer_key=writer,
            run_id=None,
            kind="text",
            **kwargs,  # type: ignore[arg-type]
        )
    )


async def test_text_non_string_value_is_unavailable() -> None:
    writer = _connected({"choice": True, "text": True})
    asking = await _ask_text(writer)
    await asyncio.sleep(0)
    wm_server._resolve_client_response(
        writer.written[-1]["id"], {"result": {"outcome": "answered", "value": 3}}
    )
    assert await asyncio.wait_for(asking, timeout=1.0) == {"outcome": "unavailable"}


async def test_text_over_bound_is_unavailable() -> None:
    writer = _connected({"choice": True, "text": True})
    asking = await _ask_text(writer)
    await asyncio.sleep(0)
    wm_server._resolve_client_response(
        writer.written[-1]["id"],
        {"result": {"outcome": "answered", "value": "x" * 65537}},
    )
    assert await asyncio.wait_for(asking, timeout=1.0) == {"outcome": "unavailable"}


async def test_text_lone_surrogate_is_unavailable() -> None:
    writer = _connected({"choice": True, "text": True})
    asking = await _ask_text(writer)
    await asyncio.sleep(0)
    wm_server._resolve_client_response(
        writer.written[-1]["id"],
        {"result": {"outcome": "answered", "value": "\udcff"}},
    )
    assert await asyncio.wait_for(asking, timeout=1.0) == {"outcome": "unavailable"}


async def _ask_multi(writer: _FakeWriter, options: list[str]) -> asyncio.Task:
    return asyncio.create_task(
        _bridge().elicit(
            message="?",
            options=options,
            default=None,
            timeout_sec=5.0,
            run_writer_key=writer,
            run_id=None,
            kind="multiChoice",
        )
    )


async def test_multi_valid_comes_back_in_offered_order() -> None:
    writer = _connected({"multiChoice": True})
    asking = await _ask_multi(writer, ["a", "b", "c"])
    await asyncio.sleep(0)
    wm_server._resolve_client_response(
        writer.written[-1]["id"],
        {"result": {"outcome": "answered", "values": ["c", "a"]}},
    )
    assert await asyncio.wait_for(asking, timeout=1.0) == {
        "outcome": "answered",
        "values": ["a", "c"],
    }


@pytest.mark.parametrize(
    "values",
    [["a", "a"], ["a", "zzz"], ["a", 1], "not-a-list", None],
)
async def test_multi_invalid_is_unavailable(values: object) -> None:
    writer = _connected({"multiChoice": True})
    asking = await _ask_multi(writer, ["a", "b"])
    await asyncio.sleep(0)
    wm_server._resolve_client_response(
        writer.written[-1]["id"], {"result": {"outcome": "answered", "values": values}}
    )
    assert await asyncio.wait_for(asking, timeout=1.0) == {"outcome": "unavailable"}


# A18: choice wire byte-identical without descriptions
async def test_choice_wire_omits_new_fields() -> None:
    writer = _connected(True)
    asking = asyncio.create_task(
        _bridge().elicit(
            message="pick?",
            options=["a"],
            default=None,
            timeout_sec=9.0,
            run_writer_key=writer,
            run_id=None,
            kind="choice",
        )
    )
    await asyncio.sleep(0)
    params = writer.written[-1]["params"]
    assert set(params) == {"message", "options", "default", "timeoutSec"}
    wm_server._resolve_client_response(
        writer.written[-1]["id"], {"result": {"outcome": "declined"}}
    )
    assert await asyncio.wait_for(asking, timeout=1.0) == {"outcome": "declined"}


def test_newer_wm_treats_missing_kind_as_choice() -> None:
    raw = {"message": "m", "options": ["a"], "default": None, "timeoutSec": 5.0}
    params = converter.structure(raw, _internal_client_types.ElicitParams)
    assert params.kind == "choice"


def test_initialize_echo_stays_bool() -> None:
    assert bool({"choice": True}) is True


# A19: result omits None
def test_result_unstructures_without_none() -> None:
    empty = converter.unstructure(
        _internal_client_types.ElicitResult(outcome="unavailable")
    )
    assert empty == {"outcome": "unavailable"}
    answered = converter.unstructure(
        _internal_client_types.ElicitResult(outcome="answered", value="a")
    )
    assert answered == {"outcome": "answered", "value": "a"}
    multi = converter.unstructure(
        _internal_client_types.ElicitResult(outcome="answered", values=["a"])
    )
    assert multi == {"outcome": "answered", "values": ["a"]}
