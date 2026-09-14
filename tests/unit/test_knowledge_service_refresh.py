"""Re-extracting a stale bucket on demand, at the WM layer that dispatches it.

The engine's half -- that a dirty bucket is refreshed before it is compared,
and that an unchanged result still cuts off -- is covered in
``finecode_knowledge/tests/test_memo_walk.py`` with a refresher that has no
idea what an ER is. This file covers the other half: dispatching a real-shaped
``extract_knowledge`` run (with the executor mocked -- there is no ER here),
ingesting what comes back, updating the freshness verdict so an answer does not
report data as stale immediately after refreshing it, and retracting a bucket
the provider no longer enumerates while *not* retracting one it merely
re-extracted.

If these regress, a user editing a file and re-running an audit gets findings
computed from what the file used to say, or loses facts for a file that still
exists.

Uses the same ``Widget`` test schema and fact-file helpers as
``test_knowledge_service.py`` rather than importing them, so this file does not
couple to that one's internals.
"""

from __future__ import annotations

import asyncio
import pathlib
import time
import typing

import pytest

from finecode.wm_server import context, domain
from finecode.wm_server.runner import runner_client
from finecode.wm_server.services import knowledge_service, run_service
from finecode_knowledge import query as q
from finecode_knowledge.fact_file import write_facts
from finecode_knowledge.model.bands import Band
from finecode_knowledge.model.entity_type import EntityType
from finecode_knowledge.model.facts import FieldFact, Provenance, RunStamp
from finecode_knowledge.model.fields import Field
from finecode_knowledge.model.fingerprint import capture
from finecode_knowledge.model.provider import EntityProvider
from finecode_knowledge.model.registry import SchemaRegistry
from finecode_knowledge.model.store import FactStore
from finecode_knowledge.model.unit import Unit
from finecode_knowledge.model.verify import Verdict
from finecode_knowledge.model.wire import fact_to_json
from finecode_knowledge.query.serialize import query_to_json, result_from_json
from finecode_knowledge.query.snapshot import registry_to_json


class WidgetFields:
    name: Field[Widget, str] = Field("name", entity="Widget")


class Widget(EntityType):
    NAME = "Widget"
    KEY = [WidgetFields.name]  # noqa: RUF012
    CORE = [WidgetFields.name]  # noqa: RUF012


class WidgetProvider(EntityProvider):
    ID = "widgets"
    SUPPLIES_FIELDS = [WidgetFields.name]  # noqa: RUF012
    SUPPLIES_EDGES: list = []  # noqa: RUF012


PROVIDER_ID = f"{__name__.partition('.')[0]}.{WidgetProvider.ID}"
BUCKET = (PROVIDER_ID, "gadget.py")
BUCKET_ID = f"{PROVIDER_ID}:gadget.py"


def _schema() -> SchemaRegistry:
    schema = SchemaRegistry()
    schema.register_entity_type(Widget)
    schema.register_namespace(WidgetFields)
    schema.register_provider(WidgetProvider)
    return schema


def _widget_fact(name: str, run_id: str) -> FieldFact:
    return FieldFact(
        entity=Widget.ref(name=name),
        field="name",
        value=name,
        prov=Provenance(
            band=Band.DECLARED,
            provider=PROVIDER_ID,
            run=RunStamp(id=run_id, observed_at="2026-08-07T00:00:00Z"),
        ),
    )


def _all_widgets() -> dict:
    widget, name = q.var(Widget), q.var(str)
    built = q.query(widget, name, schema=_schema()).where(
        WidgetFields.name(widget, name)
    )
    return query_to_json(built)


@pytest.fixture
def workspace(tmp_path: pathlib.Path) -> typing.Iterator[context.WorkspaceContext]:
    _clear_process_state()
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    root_project = domain.CollectedProject(
        name=tmp_path.name,
        dir_path=tmp_path,
        def_path=tmp_path / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
        env_configs={},
        actions=[
            domain.Action(
                name="extract_knowledge",
                source="fine_knowledge.ExtractKnowledgeAction",
                handlers=[],
                config={},
            )
        ],
        services=[],
        action_handler_configs={},
    )
    ws_context.ws_projects[tmp_path] = root_project
    yield ws_context
    _clear_process_state()


def _clear_process_state() -> None:
    knowledge_service.reset()
    knowledge_service.memo().clear()
    knowledge_service._state.schema = None
    knowledge_service._state.snapshot = None
    knowledge_service._state.walk_stats = None
    knowledge_service._refresh_inflight.clear()
    knowledge_service._state.dirty_since_persist = False
    knowledge_service._state.last_persisted_at = 0.0


def _seed(workspace: context.WorkspaceContext, *, name: str = "gadget") -> pathlib.Path:
    """A workspace with one tracked source and a fact file describing it."""
    root = workspace.ws_dirs_paths[0]
    source = root / "gadget.py"
    source.write_text("widget = 1\n")
    schema = _schema()
    store = FactStore(schema)
    store.ingest(
        PROVIDER_ID,
        [_widget_fact(name, "run-1")],
        unit=Unit(
            provider_id=PROVIDER_ID,
            unit_id="gadget.py",
            fingerprints=(capture("gadget.py", root),),
        ),
    )
    write_facts(store, root / knowledge_service.DEFAULT_FACTS_PATH)
    return root


def _stub_dispatch(
    monkeypatch: pytest.MonkeyPatch, *, buckets: list[str], facts: list[dict]
) -> list[dict]:
    """Replace the ER round trip with a canned ``ExtractKnowledgeRunResult``.

    Returns the list of payloads the (fake) dispatch was actually called
    with, so a test can assert on what the WM asked for -- scope, and the
    ``return_facts`` flag D-6 depends on.
    """
    calls: list[dict] = []

    class _FakeExecutor:
        def __init__(self, ws_context) -> None:
            self._ws_context = ws_context

        async def run_actions_in_projects(
            self, *, actions_by_project, params, **kwargs
        ):
            calls.append({"params": params, "kwargs": kwargs})
            (project_path,) = actions_by_project
            response = runner_client.RunActionResponse(
                result_by_format={
                    "json": {
                        "facts_path": "",
                        "fact_counts": {},
                        "raw_fact_counts": {},
                        "buckets": buckets,
                        "facts": facts,
                        "error": None,
                    }
                },
                return_code=0,
            )
            return {project_path: {"extract_knowledge": response}}

    monkeypatch.setattr(run_service, "WorkspaceExecutor", _FakeExecutor)
    return calls


class TestTheRefreshIsDispatchedAndIngested:
    """Acceptance criteria 1 and 2, at the layer that dispatches the run."""

    async def test_a_dirty_bucket_is_refreshed_and_the_verdict_updates(
        self, workspace: context.WorkspaceContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bucket is refreshed, its digest is unchanged (a same-content
        re-extraction -- the docstring-edit case), and D-5's report update
        means the *next* read no longer reserves it as stale."""
        root = _seed(workspace)
        await knowledge_service.register_schema(registry_to_json(_schema()))
        wire = _all_widgets()
        first = result_from_json(await knowledge_service.run_query(workspace, wire))
        assert [row[1] for row in first.rows] == ["gadget"]

        # Edit the tracked source with nothing else happening -- no fact file
        # write, no explicit invalidate/reset. This is the scenario the whole
        # plan exists for.
        (root / "gadget.py").write_text("widget = 1  # docstring\n")

        stored_unit = Unit(
            provider_id=PROVIDER_ID,
            unit_id="gadget.py",
            fingerprints=(capture("gadget.py", root),),
        )
        calls = _stub_dispatch(
            monkeypatch,
            buckets=[BUCKET_ID],
            facts=[
                {
                    "bucket": BUCKET_ID,
                    "unit": stored_unit.to_json(),
                    "facts": [fact_to_json(_widget_fact("gadget", "run-2"))],
                }
            ],
        )

        second = result_from_json(await knowledge_service.run_query(workspace, wire))

        assert calls, "the refresh was dispatched"
        assert calls[0]["params"]["buckets"] == [BUCKET_ID]
        assert calls[0]["params"]["return_facts"] is True
        assert calls[0]["kwargs"]["cancellable"] is True
        assert calls[0]["kwargs"]["orchestration_depth"] > 0, "step 6"

        stats = knowledge_service.last_walk_stats()
        assert stats.refreshes == 1
        assert stats.digest_cutoffs == 1, "same facts, different run stamp"
        assert stats.recomputes == 0
        assert [row[1] for row in second.rows] == ["gadget"]

        verdict = knowledge_service.bucket_verdict(workspace, *BUCKET)
        assert verdict.kind is Verdict.CONFIRMED, (
            "D-5: not stale over data just refreshed"
        )

    async def test_a_refresh_that_changes_the_facts_recomputes(
        self, workspace: context.WorkspaceContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other half: cutoff must not be a way of never noticing anything."""
        root = _seed(workspace)
        await knowledge_service.register_schema(registry_to_json(_schema()))
        wire = _all_widgets()
        await knowledge_service.run_query(workspace, wire)
        (root / "gadget.py").write_text("widget = 2\n")

        stored_unit = Unit(
            provider_id=PROVIDER_ID,
            unit_id="gadget.py",
            fingerprints=(capture("gadget.py", root),),
        )
        _stub_dispatch(
            monkeypatch,
            buckets=[BUCKET_ID],
            facts=[
                {
                    "bucket": BUCKET_ID,
                    "unit": stored_unit.to_json(),
                    "facts": [fact_to_json(_widget_fact("widget-v2", "run-2"))],
                }
            ],
        )

        answered = result_from_json(await knowledge_service.run_query(workspace, wire))

        stats = knowledge_service.last_walk_stats()
        assert stats.refreshes == 1
        assert stats.recomputes == 1
        assert [row[1] for row in answered.rows] == ["widget-v2"]

    async def test_nothing_changed_refreshes_nothing_on_the_second_run(
        self, workspace: context.WorkspaceContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**Acceptance criterion 3.** No dispatch at all when the world did not
        move -- the whole point of gating the refresh on the dirty bucket
        `_verify_extraction` already tracked, not on "a refresher exists"."""
        _seed(workspace)
        await knowledge_service.register_schema(registry_to_json(_schema()))
        wire = _all_widgets()
        calls = _stub_dispatch(monkeypatch, buckets=[], facts=[])
        await knowledge_service.run_query(workspace, wire)
        first_calls = len(calls)

        second = result_from_json(await knowledge_service.run_query(workspace, wire))

        stats = knowledge_service.last_walk_stats()
        assert len(calls) == first_calls, "no second dispatch"
        assert stats.refreshes == 0
        assert (stats.hits, stats.recomputes) == (1, 0)
        assert [row[1] for row in second.rows] == ["gadget"]


class TestRetractionOnSilence:
    """D-7, acceptance criteria 5 and 6."""

    async def test_silence_retracts_the_bucket(
        self, workspace: context.WorkspaceContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**Acceptance criterion 5.** Asked about the bucket, the provider
        enumerated nothing for it -- retract. The store no longer holds it,
        and asking about it again is a WM-state inconsistency (the bucket is
        simply gone), not a crash."""
        root = _seed(workspace)
        await knowledge_service.register_schema(registry_to_json(_schema()))
        wire = _all_widgets()
        await knowledge_service.run_query(workspace, wire)
        (root / "gadget.py").unlink()  # the deleted-source scenario

        _stub_dispatch(monkeypatch, buckets=[], facts=[])  # asked, got nothing back

        answered = result_from_json(await knowledge_service.run_query(workspace, wire))

        assert answered.rows == []
        store = knowledge_service.load_store(workspace)
        assert store.unit(*BUCKET) is None, "retracted, not merely reserved"

    async def test_a_re_extracted_bucket_is_never_retracted(
        self, workspace: context.WorkspaceContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**Acceptance criterion 6, D-7's rejected alternative written as a
        test.** A bucket the WM asked about and the provider *did* re-extract
        -- the shape a renamed provider module's buckets take, since renaming
        the provider's own file changes nothing about which source files it
        still enumerates -- is never retracted, regardless of why it was
        dirty (``MISSING`` fires on *any* absent declared input, including the
        provider's own code, per D-7's rejected-WM-deletes-MISSING candidate).
        """
        root = _seed(workspace)
        await knowledge_service.register_schema(registry_to_json(_schema()))
        wire = _all_widgets()
        await knowledge_service.run_query(workspace, wire)
        # Simulate the provider's own module having moved: the bucket's
        # verdict would be MISSING (a declared input gone), but the source it
        # scans is untouched, so a real provider would still enumerate it.
        knowledge_service.invalidate([BUCKET])

        stored_unit = Unit(
            provider_id=PROVIDER_ID,
            unit_id="gadget.py",
            fingerprints=(capture("gadget.py", root),),
        )
        _stub_dispatch(
            monkeypatch,
            buckets=[BUCKET_ID],
            facts=[
                {
                    "bucket": BUCKET_ID,
                    "unit": stored_unit.to_json(),
                    "facts": [fact_to_json(_widget_fact("gadget", "run-3"))],
                }
            ],
        )

        answered = result_from_json(await knowledge_service.run_query(workspace, wire))

        store = knowledge_service.load_store(workspace)
        assert store.unit(*BUCKET) is not None, "re-extracted, so it must survive"
        assert [row[1] for row in answered.rows] == ["gadget"]


class TestPersistence:
    """The second trap: a refresh must not write the whole (tens-of-MB) fact
    file on every single bucket, and the first trap: when it does write, the
    WM must not mistake its own write for a foreign out-of-band one on the
    very next read.
    """

    async def test_a_refresh_does_not_write_the_file_immediately(
        self, workspace: context.WorkspaceContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The second trap. `_maybe_persist`'s throttle means a single refresh,
        moments after the last write (here: process start, `last_persisted_at
        == 0.0`, which *is* immediately due) -- so this pins the throttle
        window open first to isolate the "does not write on every refresh"
        claim from "the first one always writes"."""
        root = _seed(workspace)
        await knowledge_service.register_schema(registry_to_json(_schema()))
        knowledge_service._state.last_persisted_at = time.monotonic()
        facts_path = root / knowledge_service.DEFAULT_FACTS_PATH
        written_at = facts_path.stat().st_mtime_ns

        stored_unit = Unit(
            provider_id=PROVIDER_ID,
            unit_id="gadget.py",
            fingerprints=(capture("gadget.py", root),),
        )
        knowledge_service.invalidate([BUCKET])
        _stub_dispatch(
            monkeypatch,
            buckets=[BUCKET_ID],
            facts=[
                {
                    "bucket": BUCKET_ID,
                    "unit": stored_unit.to_json(),
                    "facts": [fact_to_json(_widget_fact("gadget", "run-2"))],
                }
            ],
        )

        await knowledge_service.run_query(workspace, _all_widgets())

        assert facts_path.stat().st_mtime_ns == written_at, "throttled, not written yet"
        assert knowledge_service._state.dirty_since_persist is True

    async def test_persist_pending_flushes_and_restamps(
        self, workspace: context.WorkspaceContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The first trap: `persist_pending` (the shutdown path) writes
        unconditionally, and stamps `_state.facts_stamp` from that same write
        -- so the very next `VERIFIED` read does not see its own write as a
        foreign one and drop the store it just wrote."""
        root = _seed(workspace)
        await knowledge_service.register_schema(registry_to_json(_schema()))
        knowledge_service.load_store(workspace)
        # Pin the throttle open, so the refresh below leaves the write to
        # `persist_pending` rather than the (equally correct) immediate write
        # `_maybe_persist` would otherwise do on a never-yet-persisted store.
        knowledge_service._state.last_persisted_at = time.monotonic()
        stored_unit = Unit(
            provider_id=PROVIDER_ID,
            unit_id="gadget.py",
            fingerprints=(capture("gadget.py", root),),
        )
        knowledge_service.invalidate([BUCKET])
        _stub_dispatch(
            monkeypatch,
            buckets=[BUCKET_ID],
            facts=[
                {
                    "bucket": BUCKET_ID,
                    "unit": stored_unit.to_json(),
                    "facts": [fact_to_json(_widget_fact("gadget", "run-2"))],
                }
            ],
        )
        await knowledge_service.run_query(workspace, _all_widgets())
        assert knowledge_service._state.dirty_since_persist is True
        store_before = knowledge_service.load_store(workspace)

        flushed = await knowledge_service.persist_pending(workspace)

        assert flushed is True
        assert knowledge_service._state.dirty_since_persist is False
        facts_path = root / knowledge_service.DEFAULT_FACTS_PATH
        assert knowledge_service._state.facts_stamp == (
            facts_path.stat().st_mtime_ns,
            facts_path.stat().st_size,
        )

        # The next VERIFIED read must not see its own write as foreign and
        # reload -- the same store object survives.
        _stub_dispatch(monkeypatch, buckets=[], facts=[])
        await knowledge_service.run_query(workspace, _all_widgets())
        assert knowledge_service.load_store(workspace) is store_before

    async def test_persist_pending_is_a_noop_when_nothing_is_dirty(
        self, workspace: context.WorkspaceContext
    ) -> None:
        _seed(workspace)
        await knowledge_service.register_schema(registry_to_json(_schema()))
        knowledge_service.load_store(workspace)

        assert await knowledge_service.persist_pending(workspace) is False


class TestConcurrentRefreshes:
    """Two refreshes at once are the normal case, not a recursion.

    Regression cover for a real failure: admission control was a single
    process-wide "a refresh is dispatching" flag, so the first refresh held it
    for its whole ER round trip and every *sibling* refresh -- different
    bucket, different query -- was dropped. On a live workspace one
    ``audit_code`` run dropped 24 of them across 4 buckets, every one of which
    then surfaced to the user as a ``[stale]`` reservation. The answers stayed
    honest; on-demand re-extraction simply did not happen, and looked from the
    outside exactly like having had nothing to do.
    """

    async def test_two_buckets_refresh_concurrently_rather_than_one_being_dropped(
        self, workspace: context.WorkspaceContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        root = _seed(workspace)
        (root / "widget.py").write_text("widget = 2\n")
        started: list[str] = []
        release = asyncio.Event()

        class _BlockingExecutor:
            def __init__(self, ws_context) -> None: ...

            async def run_actions_in_projects(
                self, *, actions_by_project, params, **kwargs
            ):
                started.append(params["buckets"][0])
                # Hold the dispatch open, which is exactly what the process-wide
                # flag turned into a refusal for every other bucket.
                await release.wait()
                (project_path,) = actions_by_project
                return {
                    project_path: {
                        "extract_knowledge": runner_client.RunActionResponse(
                            result_by_format={"json": {"buckets": [], "facts": []}},
                            return_code=0,
                        )
                    }
                }

        monkeypatch.setattr(run_service, "WorkspaceExecutor", _BlockingExecutor)

        first = asyncio.create_task(
            knowledge_service._refresh(workspace, (PROVIDER_ID, "gadget.py"))
        )
        second = asyncio.create_task(
            knowledge_service._refresh(workspace, (PROVIDER_ID, "widget.py"))
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert sorted(started) == [
            f"{PROVIDER_ID}:gadget.py",
            f"{PROVIDER_ID}:widget.py",
        ], "a refresh of a different bucket must not be dropped by one already running"

        release.set()
        await asyncio.gather(first, second)

    async def test_the_same_bucket_twice_costs_one_dispatch(
        self, workspace: context.WorkspaceContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other half: dedupe must still hold, and across queries.

        ``MemoWalk`` dedupes per bucket too, but a walk is built per query, so
        only shared state here can join two concurrent queries onto one run.
        """

        _seed(workspace)
        started: list[str] = []
        release = asyncio.Event()

        class _BlockingExecutor:
            def __init__(self, ws_context) -> None: ...

            async def run_actions_in_projects(
                self, *, actions_by_project, params, **kwargs
            ):
                started.append(params["buckets"][0])
                await release.wait()
                (project_path,) = actions_by_project
                return {
                    project_path: {
                        "extract_knowledge": runner_client.RunActionResponse(
                            result_by_format={"json": {"buckets": [], "facts": []}},
                            return_code=0,
                        )
                    }
                }

        monkeypatch.setattr(run_service, "WorkspaceExecutor", _BlockingExecutor)

        bucket = (PROVIDER_ID, "gadget.py")
        first = asyncio.create_task(knowledge_service._refresh(workspace, bucket))
        second = asyncio.create_task(knowledge_service._refresh(workspace, bucket))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert started == [f"{PROVIDER_ID}:gadget.py"], (
            "two queries needing one bucket must share a single ER round trip"
        )

        release.set()
        await asyncio.gather(first, second)


class TestTheWriteDoesNotStallTheServer:
    """Writing the fact file must not stop the WM answering everyone else.

    The WM is one event loop. Serializing the whole store is not incremental
    and measured ~1.1s on a 30MB fact file, so doing it inline is over a
    second in which nothing is answered -- no LSP request, no MCP call, and
    not the callback an Extension Runner makes *during the extraction being
    recorded*, which has its own timeout and fails the refresh it was serving.
    """

    async def test_other_work_proceeds_while_the_fact_file_is_written(
        self, workspace: context.WorkspaceContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        _seed(workspace)
        await knowledge_service.register_schema(registry_to_json(_schema()))
        knowledge_service.load_store(workspace)
        knowledge_service._state.dirty_since_persist = True

        def _slow_write(store, path) -> int:
            # Stands in for serializing a real fact file: synchronous, and long
            # enough that a caller blocked on it is unmistakable.
            time.sleep(0.2)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}")
            return 0

        monkeypatch.setattr(knowledge_service, "write_facts", _slow_write)

        served = 0

        async def _other_client() -> None:
            nonlocal served
            while True:
                served += 1
                await asyncio.sleep(0.01)

        other = asyncio.create_task(_other_client())
        await knowledge_service.persist_pending(workspace)
        other.cancel()

        assert served > 1, (
            "the event loop served nothing while the fact file was written; "
            "every other WM client is stalled for the length of the write"
        )
