"""The WM asks one client a question and always stops waiting (ADR-0082).

Three properties are load-bearing and none of them is visible from the happy
path:

* an ``id``-bearing message with no ``method`` is an *answer*, and answering it
  with "invalid request: no method" — which is what the dispatch loop did before
  this direction existed — would be a response to a response;
* an answer that arrives after the deadline is dropped rather than applied;
* losing the addressed client resolves the question at once, because the WM
  stops ~30s after its last client disconnects (ADR-0004) and a wait longer than
  that outlives the server holding the question.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from finecode.wm_server import context, wm_server
from finecode.wm_server.runner import elicitation_bridge


class _FakeWriter:
    """Captures what the server writes; stands in for a client connection."""

    def __init__(self) -> None:
        self.written: list[dict] = []
        self.closed = False

    def write(self, data: bytes) -> None:  # pragma: no cover - framing unused here
        raise AssertionError("tests capture through _write_message instead")

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def get_extra_info(self, _name: str) -> str:
        return "test-peer"


@pytest.fixture(autouse=True)
def capture_writes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, dict]]:
    written: list[tuple[object, dict]] = []

    def _capture(writer: object, msg: dict) -> None:
        written.append((writer, msg))
        if isinstance(writer, _FakeWriter):
            writer.written.append(msg)

    monkeypatch.setattr(wm_server, "_write_message", _capture)
    return written


@pytest.fixture(autouse=True)
def clean_registries():
    yield
    wm_server._pending_client_requests.clear()
    wm_server._pending_client_request_owners.clear()
    wm_server._client_capabilities.clear()
    wm_server._connected_clients.clear()
    elicitation_bridge.reset_origins()


def _feed(reader: asyncio.StreamReader, msg: dict) -> None:
    body = json.dumps(msg).encode()
    reader.feed_data(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)


async def test_a_response_is_routed_and_not_answered_with_an_error() -> None:
    """The dispatch loop must recognise an answer to its own question."""
    wm_server._keep_alive = True  # no auto-stop task from this fake disconnect
    reader = asyncio.StreamReader()
    writer = _FakeWriter()
    ws_context = context.WorkspaceContext([])

    handling = asyncio.create_task(wm_server._handle_client(reader, writer, ws_context))
    await asyncio.sleep(0)

    future: asyncio.Future = asyncio.get_running_loop().create_future()
    wm_server._pending_client_requests[4242] = future
    wm_server._pending_client_request_owners[4242] = writer

    _feed(reader, {"jsonrpc": "2.0", "id": 4242, "result": {"outcome": "declined"}})
    reader.feed_eof()
    await asyncio.wait_for(handling, timeout=2.0)

    assert future.done()
    assert future.result()["result"] == {"outcome": "declined"}
    assert writer.written == [], "an answer must not be answered"


async def test_a_message_with_neither_id_nor_method_is_ignored() -> None:
    wm_server._keep_alive = True
    reader = asyncio.StreamReader()
    writer = _FakeWriter()

    handling = asyncio.create_task(
        wm_server._handle_client(reader, writer, context.WorkspaceContext([]))
    )
    await asyncio.sleep(0)
    _feed(reader, {"jsonrpc": "2.0"})
    reader.feed_eof()
    await asyncio.wait_for(handling, timeout=2.0)

    assert writer.written == []


async def test_a_late_answer_is_discarded() -> None:
    """The future is off the registry by the time the answer lands."""
    writer = _FakeWriter()

    with pytest.raises(wm_server.ClientRequestFailed, match="did not answer"):
        await wm_server._request_client(
            writer, "client/elicit", {"message": "?"}, timeout_sec=0.01
        )

    assert wm_server._pending_client_requests == {}
    assert wm_server._pending_client_request_owners == {}

    # The client answers anyway. Nothing to resolve, nothing to crash on, and
    # nothing written back at it.
    wm_server._resolve_client_response(1, {"result": {"outcome": "answered"}})
    assert writer.written[-1]["method"] == "client/elicit"


async def test_losing_the_client_resolves_the_question_at_once() -> None:
    """Not after the deadline: the server would be gone before then."""
    writer = _FakeWriter()

    asking = asyncio.create_task(
        wm_server._request_client(
            writer, "client/elicit", {"message": "?"}, timeout_sec=300.0
        )
    )
    await asyncio.sleep(0)
    assert len(wm_server._pending_client_requests) == 1

    wm_server._fail_pending_requests_for(writer)

    with pytest.raises(wm_server.ClientRequestFailed, match="disconnected"):
        await asyncio.wait_for(asking, timeout=1.0)
    assert wm_server._pending_client_requests == {}


async def test_a_client_cannot_answer_a_question_put_to_another_client() -> None:
    """Ids come from one counter shared by every connection (ADR-0082 rule 1).

    Without an owner check, any connected client could resolve somebody else's
    question by guessing an id, and the asking run would act on it.
    """
    addressed = _FakeWriter()
    other = _FakeWriter()

    asking = asyncio.create_task(
        wm_server._request_client(
            addressed, "client/elicit", {"message": "?"}, timeout_sec=300.0
        )
    )
    await asyncio.sleep(0)
    request_id = addressed.written[-1]["id"]

    wm_server._resolve_client_response(
        request_id, {"result": {"outcome": "answered", "value": "revert"}}, other
    )
    await asyncio.sleep(0)
    assert not asking.done(), "an answer from the wrong client was accepted"

    wm_server._resolve_client_response(
        request_id, {"result": {"outcome": "declined"}}, addressed
    )
    assert await asyncio.wait_for(asking, timeout=1.0) == {"outcome": "declined"}


async def test_a_client_declaring_null_capabilities_still_gets_its_response() -> None:
    """An explicit JSON null is not an absent key, and must not kill the connection.

    The dispatch loop owns the response to `client/initialize`; an exception
    escaping it leaves the client waiting on a request nobody will ever answer.
    """
    wm_server._keep_alive = True
    reader = asyncio.StreamReader()
    writer = _FakeWriter()

    handling = asyncio.create_task(
        wm_server._handle_client(reader, writer, context.WorkspaceContext([]))
    )
    await asyncio.sleep(0)
    _feed(
        reader,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "client/initialize",
            "params": {"clientId": "cli", "capabilities": None},
        },
    )
    reader.feed_eof()
    await asyncio.wait_for(handling, timeout=2.0)

    assert writer.written[-1]["id"] == 1
    assert writer.written[-1]["result"]["capabilities"] == {"elicitation": False}


async def test_only_the_disconnected_client_s_questions_are_resolved() -> None:
    gone = _FakeWriter()
    staying = _FakeWriter()

    asking_gone = asyncio.create_task(
        wm_server._request_client(gone, "client/elicit", {}, timeout_sec=300.0)
    )
    asking_staying = asyncio.create_task(
        wm_server._request_client(staying, "client/elicit", {}, timeout_sec=300.0)
    )
    await asyncio.sleep(0)

    wm_server._fail_pending_requests_for(gone)

    with pytest.raises(wm_server.ClientRequestFailed):
        await asyncio.wait_for(asking_gone, timeout=1.0)
    assert not asking_staying.done()
    assert len(wm_server._pending_client_requests) == 1

    asking_staying.cancel()
    await asyncio.gather(asking_staying, return_exceptions=True)


# ---------------------------------------------------------------------------
# The bridge: capability gating and outcome mapping
# ---------------------------------------------------------------------------


async def test_a_client_that_declared_nothing_is_never_asked() -> None:
    """Rule 2: unanswerability is declared, so it costs no round trip."""
    writer = _FakeWriter()
    wm_server._connected_clients.add(writer)
    bridge = wm_server._WmElicitationBridge()

    answer = await bridge.elicit(
        message="?",
        options=["a", "b"],
        default=None,
        timeout_sec=300.0,
        run_writer_key=writer,
    )

    assert answer == {"outcome": "unavailable"}
    assert writer.written == [], "nothing may go out to a client that cannot answer"


async def test_a_run_with_no_originating_client_is_told_immediately() -> None:
    bridge = wm_server._WmElicitationBridge()
    answer = await bridge.elicit(
        message="?",
        options=["a"],
        default=None,
        timeout_sec=300.0,
        run_writer_key=None,
    )
    assert answer == {"outcome": "unavailable"}


async def test_an_answered_question_comes_back_with_its_value() -> None:
    writer = _FakeWriter()
    wm_server._connected_clients.add(writer)
    wm_server._client_capabilities[writer] = {"elicitation": {"choice": True}}
    bridge = wm_server._WmElicitationBridge()

    asking = asyncio.create_task(
        bridge.elicit(
            message="keep it?",
            options=["keep", "revert"],
            default="keep",
            timeout_sec=300.0,
            run_writer_key=writer,
        )
    )
    await asyncio.sleep(0)

    sent = writer.written[-1]
    assert sent["method"] == "client/elicit"
    assert sent["params"]["options"] == ["keep", "revert"]
    wm_server._resolve_client_response(
        sent["id"], {"result": {"outcome": "answered", "value": "revert"}}
    )

    assert await asyncio.wait_for(asking, timeout=1.0) == {
        "outcome": "answered",
        "value": "revert",
    }


async def test_an_answer_nobody_offered_is_not_an_answer() -> None:
    writer = _FakeWriter()
    wm_server._connected_clients.add(writer)
    wm_server._client_capabilities[writer] = {"elicitation": True}
    bridge = wm_server._WmElicitationBridge()

    asking = asyncio.create_task(
        bridge.elicit(
            message="?",
            options=["keep"],
            default=None,
            timeout_sec=300.0,
            run_writer_key=writer,
        )
    )
    await asyncio.sleep(0)
    wm_server._resolve_client_response(
        writer.written[-1]["id"],
        {"result": {"outcome": "answered", "value": "something else"}},
    )

    assert await asyncio.wait_for(asking, timeout=1.0) == {"outcome": "unavailable"}


async def test_a_person_who_declined_is_not_a_person_who_was_never_there() -> None:
    writer = _FakeWriter()
    wm_server._connected_clients.add(writer)
    wm_server._client_capabilities[writer] = {"elicitation": True}
    bridge = wm_server._WmElicitationBridge()

    asking = asyncio.create_task(
        bridge.elicit(
            message="?",
            options=["keep"],
            default=None,
            timeout_sec=300.0,
            run_writer_key=writer,
        )
    )
    await asyncio.sleep(0)
    wm_server._resolve_client_response(
        writer.written[-1]["id"], {"result": {"outcome": "declined"}}
    )

    assert await asyncio.wait_for(asking, timeout=1.0) == {"outcome": "declined"}


# ---------------------------------------------------------------------------
# Addressing
# ---------------------------------------------------------------------------


def test_the_origin_is_only_recorded_for_the_duration_of_the_run() -> None:
    writer = _FakeWriter()
    assert elicitation_bridge.originating_client_for_run("run-1") is None

    with (
        elicitation_bridge.originating_client(writer),
        elicitation_bridge.bind_run("run-1"),
    ):
        assert elicitation_bridge.originating_client_for_run("run-1") is writer

    assert elicitation_bridge.originating_client_for_run("run-1") is None


def test_a_run_nobody_started_is_bound_to_nobody() -> None:
    """A dispatch the WM made on its own behalf has no person behind it.

    Binding it to whichever client happened to be around would put a question in
    front of someone who never asked for the work.
    """
    with elicitation_bridge.bind_run("run-1"):
        assert elicitation_bridge.originating_client_for_run("run-1") is None


def test_two_clients_running_at_once_are_told_apart() -> None:
    """The reason addressing is by run and not by project.

    Two people can be running the same project at the same moment. Answering
    either one's question with the other's terminal is worse than not asking:
    the answer is applied to work that person never started.
    """
    first = _FakeWriter()
    second = _FakeWriter()

    with (
        elicitation_bridge.originating_client(first),
        elicitation_bridge.bind_run("run-first"),
        # The second client starts while the first is still running, which is
        # the case the registry has to keep apart.
        elicitation_bridge.originating_client(second),
        elicitation_bridge.bind_run("run-second"),
    ):
        assert elicitation_bridge.originating_client_for_run("run-first") is first
        assert elicitation_bridge.originating_client_for_run("run-second") is second


def test_a_run_the_er_cannot_name_is_addressed_to_nobody() -> None:
    """An ER that predates the run id sends none, and is told so at once."""
    writer = _FakeWriter()
    with (
        elicitation_bridge.originating_client(writer),
        elicitation_bridge.bind_run("run-1"),
    ):
        assert elicitation_bridge.originating_client_for_run(None) is None
