"""The WM's knowledge home (memo-dag-plan Phases 0 and 1, R13b).

Phase 0's "done when" is two claims: the WM holds a store, and it can report a
bucket verdict. Phase 1 adds the two that make `goals.md` §4.10 real: the WM is
*handed* a schema rather than importing one (D-7), and it executes queries an ER
sends rather than the ER reading facts itself.

All of it is asserted against a fact file written by the engine, with no ER and
no extension package involved -- which is itself part of the point, since the WM
importing `fine_knowledge` is what D-1 forbids.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import typing

import pytest

from finecode.wm_server import context
from finecode.wm_server.errors import FactsNotExtractedError, InternalError
from finecode.wm_server.services import knowledge_service
from finecode_knowledge import query as q
from finecode_knowledge.fact_file import write_facts
from finecode_knowledge.model.bands import Band
from finecode_knowledge.model.entity_type import EntityType
from finecode_knowledge.model.facts import FieldFact, Provenance, RunStamp, SourceLoc
from finecode_knowledge.model.fields import Field
from finecode_knowledge.model.fingerprint import capture
from finecode_knowledge.model.provider import EntityProvider
from finecode_knowledge.model.registry import SchemaRegistry
from finecode_knowledge.model.store import FactStore
from finecode_knowledge.model.unit import Unit
from finecode_knowledge.model.verify import Verdict
from finecode_knowledge.query.interpret import InterpreterBackend
from finecode_knowledge.query.records import records_from_json, refs_to_json
from finecode_knowledge.query.serialize import (
    query_from_json,
    query_to_json,
    result_from_json,
)
from finecode_knowledge.query.snapshot import SnapshotError, registry_to_json

# --- a schema of the test's own, so nothing here depends on FineCode's --------


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
"""The provider's qualified id -- there is no unqualified spelling (ADR-0017 D4).

Derived from ``__name__`` rather than written out, because the qualifier is this
module's top-level package (D3) and pytest imports a test module under a name
that depends on how it was collected.
"""


def _schema() -> SchemaRegistry:
    schema = SchemaRegistry()
    schema.register_entity_type(Widget)
    schema.register_namespace(WidgetFields)
    schema.register_provider(WidgetProvider)
    return schema


def _write_fact_file(
    workspace_root: pathlib.Path, source: pathlib.Path
) -> pathlib.Path:
    """A one-bucket fact file whose single tracked input is *source*."""
    schema = _schema()
    store = FactStore(schema)
    ref = Widget.ref(name="gadget")
    prov = Provenance(
        band=Band.DECLARED,
        provider=PROVIDER_ID,
        run=RunStamp(id="run-1", observed_at="2026-07-30T00:00:00Z"),
        location=SourceLoc(project=None, file=source.name, line=1),
    )
    store.ingest(
        PROVIDER_ID,
        [FieldFact(entity=ref, field="name", value="gadget", prov=prov)],
        unit=Unit(
            provider_id=PROVIDER_ID,
            unit_id=str(source.name),
            fingerprints=(capture(source.name, workspace_root),),
        ),
    )
    facts_path = workspace_root / knowledge_service.DEFAULT_FACTS_PATH
    write_facts(store, facts_path)
    return facts_path


def _write_fact_file_as(
    workspace_root: pathlib.Path, source: pathlib.Path, name: str
) -> pathlib.Path:
    """Like ``_write_fact_file``, but the single widget is named *name*.

    Used to simulate an out-of-band ``extract_knowledge`` re-extracting the
    *same* bucket with different content -- as opposed to ``_write_fact_file``,
    which always writes ``"gadget"``. A query memoized against the old content
    only recomputes when the bucket it actually reads changes; a query result
    that never checked ``gadget.py`` at all cannot be expected to notice a
    brand-new, never-before-seen bucket appearing elsewhere (that is a property
    of the memo DAG's footprint tracking, unrelated to this phase).
    """
    schema = _schema()
    store = FactStore(schema)
    ref = Widget.ref(name=name)
    prov = Provenance(
        band=Band.DECLARED,
        provider=PROVIDER_ID,
        run=RunStamp(id="run-2", observed_at="2026-07-30T01:00:00Z"),
        location=SourceLoc(project=None, file=source.name, line=1),
    )
    store.ingest(
        PROVIDER_ID,
        [FieldFact(entity=ref, field="name", value=name, prov=prov)],
        unit=Unit(
            provider_id=PROVIDER_ID,
            unit_id=str(source.name),
            fingerprints=(capture(source.name, workspace_root),),
        ),
    )
    facts_path = workspace_root / knowledge_service.DEFAULT_FACTS_PATH
    write_facts(store, facts_path)
    return facts_path


@pytest.fixture
def workspace(tmp_path: pathlib.Path) -> typing.Iterator[context.WorkspaceContext]:
    _clear_process_state()
    yield context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    _clear_process_state()


def _clear_process_state() -> None:
    """Both singletons, because both outlive a test.

    The memo table is deliberately process-lifetime state (D-4), so without this
    a query memoized by one test is *served* to the next -- which reads as a
    passing test right up until the one that asserts a recomputation happened.
    """
    knowledge_service.reset()
    knowledge_service.memo().clear()
    knowledge_service._state.schema = None
    knowledge_service._state.snapshot = None
    knowledge_service._state.walk_stats = None


def _populated(workspace: context.WorkspaceContext) -> pathlib.Path:
    """A workspace with one source file and a fact file describing it."""
    root = workspace.ws_dirs_paths[0]
    source = root / "gadget.py"
    source.write_text("widget = 1\n")
    _write_fact_file(root, source)
    return root


def _all_widgets(schema: SchemaRegistry | None = None) -> dict:
    """A serialized query for every widget name -- what an ER would put on the wire.

    Built against a registry of the caller's own, exactly as a rule module would:
    the point of the crossing is that the two sides never share the object.
    """
    widget, name = q.var(Widget), q.var(str)
    built = q.query(widget, name, schema=schema or _schema()).where(
        WidgetFields.name(widget, name)
    )
    return query_to_json(built)


class TestLoadingTheStore:
    def test_the_wm_holds_a_store_read_from_the_workspace_fact_file(
        self, workspace: context.WorkspaceContext
    ) -> None:
        root = workspace.ws_dirs_paths[0]
        source = root / "gadget.py"
        source.write_text("widget = 1\n")
        _write_fact_file(root, source)

        store = knowledge_service.load_store(workspace)

        assert [unit.unit_id for unit in store.units()] == ["gadget.py"]

    def test_the_store_is_read_once_and_held_for_the_process(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """Phase 2 hangs the memo table off this object, so it has to be one object."""
        root = workspace.ws_dirs_paths[0]
        source = root / "gadget.py"
        source.write_text("widget = 1\n")
        _write_fact_file(root, source)

        assert knowledge_service.load_store(workspace) is knowledge_service.load_store(
            workspace
        )

    def test_a_missing_fact_file_says_to_run_extraction_rather_than_failing_obscurely(
        self, workspace: context.WorkspaceContext
    ) -> None:
        with pytest.raises(FactsNotExtractedError) as excinfo:
            knowledge_service.load_store(workspace)
        assert "extract_knowledge" in str(excinfo.value)

    def test_a_workspace_with_no_root_directory_is_an_internal_error(self) -> None:
        """Nothing to resolve the relative fact path against -- WM state, not user input."""
        with pytest.raises(InternalError):
            knowledge_service.facts_file_path(
                context.WorkspaceContext(ws_dirs_paths=[])
            )


class TestReportingABucketVerdict:
    def test_an_untouched_source_leaves_its_bucket_confirmed(
        self, workspace: context.WorkspaceContext
    ) -> None:
        root = workspace.ws_dirs_paths[0]
        source = root / "gadget.py"
        source.write_text("widget = 1\n")
        _write_fact_file(root, source)

        verdict = knowledge_service.bucket_verdict(workspace, PROVIDER_ID, "gadget.py")

        assert verdict.kind is Verdict.CONFIRMED

    def test_an_edited_source_makes_its_bucket_stale(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """The walk is R11's, unchanged -- this asserts the WM reaches it, not that it works."""
        root = workspace.ws_dirs_paths[0]
        source = root / "gadget.py"
        source.write_text("widget = 1\n")
        _write_fact_file(root, source)
        source.write_text("widget = 2\n")

        verdict = knowledge_service.bucket_verdict(workspace, PROVIDER_ID, "gadget.py")

        assert verdict.kind is Verdict.STALE

    def test_a_deleted_source_makes_its_bucket_missing_not_stale(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """The distinction a future retraction acts on (§4.7) survives the trip through the WM."""
        root = workspace.ws_dirs_paths[0]
        source = root / "gadget.py"
        source.write_text("widget = 1\n")
        _write_fact_file(root, source)
        source.unlink()

        verdict = knowledge_service.bucket_verdict(workspace, PROVIDER_ID, "gadget.py")

        assert verdict.kind is Verdict.MISSING

    def test_asking_twice_walks_once(self, workspace: context.WorkspaceContext) -> None:
        root = workspace.ws_dirs_paths[0]
        source = root / "gadget.py"
        source.write_text("widget = 1\n")
        _write_fact_file(root, source)

        first = knowledge_service.verify(workspace)
        second = knowledge_service.verify(workspace)

        assert first is second

    def test_a_bucket_the_store_never_held_is_an_internal_error(
        self, workspace: context.WorkspaceContext
    ) -> None:
        root = workspace.ws_dirs_paths[0]
        source = root / "gadget.py"
        source.write_text("widget = 1\n")
        _write_fact_file(root, source)

        with pytest.raises(InternalError):
            knowledge_service.bucket_verdict(workspace, PROVIDER_ID, "no_such_unit.py")


class TestTheSchemaArrivesRatherThanBeingImported:
    """D-7: the WM is handed a registry; it never reaches for a schema module."""

    def test_the_confirmation_walk_needs_no_schema_at_all(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """Which is why Phase 0 lands a phase before the ER registry snapshot does."""
        root = workspace.ws_dirs_paths[0]
        source = root / "gadget.py"
        source.write_text("widget = 1\n")
        _write_fact_file(root, source)

        assert knowledge_service._state.schema is None
        assert (
            knowledge_service.bucket_verdict(workspace, PROVIDER_ID, "gadget.py").kind
            is Verdict.CONFIRMED
        )

    def test_handing_over_a_schema_rebinds_the_store_to_it(
        self, workspace: context.WorkspaceContext
    ) -> None:
        root = workspace.ws_dirs_paths[0]
        source = root / "gadget.py"
        source.write_text("widget = 1\n")
        _write_fact_file(root, source)
        knowledge_service.load_store(workspace)

        schema = _schema()
        knowledge_service.set_schema(schema)

        assert knowledge_service.load_store(workspace).schema is schema

    def test_the_wm_reads_the_fact_file_without_importing_the_schema_package(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """The packaging boundary D-1 draws, asserted where it is load-bearing.

        `fine_knowledge` holds FineCode's schema, providers and rule code. If it
        were reachable from here, D-1's "the WM cannot import a rule because the
        distribution holding rules is not installed in its environment" would be
        a claim about deployment rather than a property of the code.

        Read from the source text, not the import graph: the three back-edges
        this split cut were all *deferred* imports inside function bodies, which
        a passing import graph never traverses. Prose is exempt -- the module
        docstring names `fine_knowledge` to say what it must not do.
        """
        source_text = pathlib.Path(knowledge_service.__file__).read_text()

        assert not re.search(
            r"^\s*(?:from|import)\s+fine_knowledge\b", source_text, re.MULTILINE
        )


class TestRevisionPinning:
    def test_the_loaded_store_pins_the_fact_files_own_digest(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """ADR-0013 D6.2: the revision identifies exactly the facts served."""
        root = workspace.ws_dirs_paths[0]
        source = root / "gadget.py"
        source.write_text("widget = 1\n")
        facts_path = _write_fact_file(root, source)

        revision = knowledge_service.load_store(workspace).revision

        assert revision == hashlib.sha256(facts_path.read_bytes()).hexdigest()

    def test_reset_lets_a_rewritten_fact_file_be_picked_up(
        self, workspace: context.WorkspaceContext
    ) -> None:
        root = workspace.ws_dirs_paths[0]
        source = root / "gadget.py"
        source.write_text("widget = 1\n")
        facts_path = _write_fact_file(root, source)
        before = knowledge_service.load_store(workspace).revision

        data = json.loads(facts_path.read_text())
        data["runs"][0]["at"] = "2026-07-31T00:00:00Z"
        facts_path.write_text(json.dumps(data))
        knowledge_service.reset()

        assert knowledge_service.load_store(workspace).revision != before


class TestRegisteringASchema:
    """Phase 1.2: the schema arrives as a snapshot, over the wire."""

    async def test_a_snapshot_becomes_the_schema_the_store_is_read_against(
        self, workspace: context.WorkspaceContext
    ) -> None:
        _populated(workspace)

        accepted = await knowledge_service.register_schema(registry_to_json(_schema()))

        assert accepted
        registry = knowledge_service.schema()
        assert registry is not None
        assert registry.entity_type(Widget.qualified_name()).NAME == "Widget"

    async def test_registering_the_same_schema_twice_costs_nothing(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """Two runners hosting one schema send the same snapshot. Rebuilding on the
        second would drop the loaded store and its verdict for no reason."""
        _populated(workspace)
        snapshot = registry_to_json(_schema())
        await knowledge_service.register_schema(snapshot)
        first = knowledge_service.load_store(workspace)

        accepted = await knowledge_service.register_schema(snapshot)

        assert accepted is False
        assert knowledge_service.load_store(workspace) is first

    async def test_an_unreadable_snapshot_fails_at_registration_not_at_the_first_query(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """An ER that thinks the WM holds its schema and finds it does not would fail
        later, at a query, with nothing pointing back here."""
        with pytest.raises(SnapshotError):
            await knowledge_service.register_schema({"v": 99})

        assert knowledge_service.schema() is None

    async def test_the_snapshot_carries_no_python_the_wm_could_run(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """D-7's actual guarantee. The WM holds a *declaration* of what each provider
        may supply; it never holds an extractor, and asking one to report its code
        inputs raises rather than returning something plausible."""
        await knowledge_service.register_schema(registry_to_json(_schema()))
        registry = knowledge_service.schema()
        assert registry is not None

        with pytest.raises(SnapshotError):
            registry.provider(PROVIDER_ID).source_inputs(registry)


class TestExecutingAQuery:
    """Phase 1.3/1.5: one query in, one result out, footprint captured here."""

    async def test_a_serialized_query_returns_rows_and_a_verdict(
        self, workspace: context.WorkspaceContext
    ) -> None:
        _populated(workspace)
        await knowledge_service.register_schema(registry_to_json(_schema()))

        answered = await knowledge_service.run_query(workspace, _all_widgets())

        result = result_from_json(answered)
        assert [row[1] for row in result.rows] == ["gadget"]
        assert result.freshness.revision

    async def test_the_wm_path_and_the_in_process_path_agree(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """The equivalence the split rests on: shipping the query must not change the
        answer, or "who executes" becomes a semantic decision rather than a placement
        one."""
        root = _populated(workspace)
        schema = _schema()
        await knowledge_service.register_schema(registry_to_json(schema))
        wire = _all_widgets(schema)

        through_wm = result_from_json(
            await knowledge_service.run_query(workspace, wire)
        )
        in_process = await InterpreterBackend(
            knowledge_service.load_store(workspace),
            schema=schema,
            verdicts=knowledge_service.verify(workspace),
        ).run(query_from_json(wire, schema), mode=q.Mode.VERIFIED)

        assert through_wm.rows == in_process.value
        assert through_wm.freshness == in_process.freshness
        assert root.exists()

    async def test_the_verdict_reports_a_stale_input_through_the_wire(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """R11's walk is what makes ``verified`` mean anything, and it has to survive
        the crossing -- a result that arrived clean over a changed source would be
        wrong in the silent direction."""
        root = _populated(workspace)
        await knowledge_service.register_schema(registry_to_json(_schema()))
        (root / "gadget.py").write_text("widget = 2\n")

        result = result_from_json(
            await knowledge_service.run_query(workspace, _all_widgets())
        )

        assert [r.kind.value for r in result.freshness.reservations] == ["stale"]
        assert not result.freshness.verified

    async def test_the_footprint_is_captured_here_and_does_not_cross(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """R7/R21 stop being a convention: the reads happen in this process, so the
        collector is here (ADR-0013 D5) and the result carries no footprint for the
        asking side to misuse."""
        _populated(workspace)
        await knowledge_service.register_schema(registry_to_json(_schema()))

        answered = await knowledge_service.run_query(workspace, _all_widgets())

        footprint = knowledge_service.last_footprint()
        assert footprint is not None and len(footprint) == 1
        assert set(answered) == {"rows", "freshness"}

    async def test_a_cached_read_with_an_empty_memo_computes(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """``Mode.CACHED`` is "serve what you have", and a WM that has just started
        has nothing. It blocks and computes rather than returning an empty answer
        fast -- and the value it produces was verified, so nothing is reserved."""
        _populated(workspace)
        await knowledge_service.register_schema(registry_to_json(_schema()))

        cached = result_from_json(
            await knowledge_service.run_query(workspace, _all_widgets(), mode="cached")
        )

        assert [row[1] for row in cached.rows] == ["gadget"]
        assert all(r.kind.value != "cached" for r in cached.freshness.reservations)

    async def test_querying_before_a_schema_arrives_says_what_is_missing(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """Rather than falling back to the empty registry the confirmation walk uses:
        that registry cannot resolve an entity type, so the query would fail somewhere
        unhelpful instead of here."""
        _populated(workspace)

        with pytest.raises(InternalError, match="registerSchema"):
            await knowledge_service.run_query(workspace, _all_widgets())

    async def test_querying_without_a_fact_file_says_to_run_extraction(
        self, workspace: context.WorkspaceContext
    ) -> None:
        await knowledge_service.register_schema(registry_to_json(_schema()))

        with pytest.raises(FactsNotExtractedError):
            await knowledge_service.run_query(workspace, _all_widgets())


class TestReadingRecords:
    """Phase 1b: the read a query cannot express, routed through the owner (R21).

    A whole-entity read quantifies over field *names*, which conjunctive queries
    have no way to say -- and R18/R19 keep the field set open, so a projection
    enumerating what it knew about would silently drop a third party's field.
    Before this message existed the consumer held a ``FactSource`` and read it
    directly, contributing no footprint key and so never being invalidated.
    """

    async def _ready(self, workspace: context.WorkspaceContext) -> None:
        _populated(workspace)
        await knowledge_service.register_schema(registry_to_json(_schema()))

    async def test_a_record_comes_back_with_its_fields_and_provenance(
        self, workspace: context.WorkspaceContext
    ) -> None:
        await self._ready(workspace)

        payload = await knowledge_service.fetch_records(
            workspace, refs_to_json([Widget.ref(name="gadget")])
        )

        (record,) = records_from_json(payload)
        assert [v.value for v in record.fields.values()] == ["gadget"]
        assert all(v.prov.provider == PROVIDER_ID for v in record.fields.values())

    async def test_many_refs_are_one_call_and_the_reply_is_positional(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """A miss yields an empty record rather than being dropped: omitting it
        would make the reply's length depend on the store's contents, and every
        caller would have to re-zip by hand."""
        await self._ready(workspace)

        payload = await knowledge_service.fetch_records(
            workspace,
            refs_to_json([Widget.ref(name="gadget"), Widget.ref(name="absent")]),
        )

        found = records_from_json(payload)
        assert len(found) == 2
        assert found[1].fields == {}

    async def test_the_read_is_captured_in_a_footprint_here(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """**The point of the message.** The key lands on the side that did the
        reading, so a projection resting on it can be invalidated on the same
        terms a rule is. Nothing memoizes a projection yet, which is the right
        order: the read stops bypassing the channel before anything depends on it.
        """
        await self._ready(workspace)

        await knowledge_service.fetch_records(
            workspace, refs_to_json([Widget.ref(name="gadget")])
        )

        footprint = knowledge_service.last_footprint()
        assert footprint is not None
        assert footprint.keys == (("entity", Widget.ref(name="gadget")),)

    async def test_reading_records_before_a_schema_arrives_says_what_is_missing(
        self, workspace: context.WorkspaceContext
    ) -> None:
        _populated(workspace)

        with pytest.raises(InternalError, match="registerSchema"):
            await knowledge_service.fetch_records(
                workspace, refs_to_json([Widget.ref(name="gadget")])
            )

    async def test_reading_records_without_a_fact_file_says_to_run_extraction(
        self, workspace: context.WorkspaceContext
    ) -> None:
        await knowledge_service.register_schema(registry_to_json(_schema()))

        with pytest.raises(FactsNotExtractedError):
            await knowledge_service.fetch_records(
                workspace, refs_to_json([Widget.ref(name="gadget")])
            )


class TestMemoizing:
    """Phases 2-4 through the WM: the DAG lives beside the store it belongs to."""

    async def _ready(self, workspace: context.WorkspaceContext) -> pathlib.Path:
        root = _populated(workspace)
        await knowledge_service.register_schema(registry_to_json(_schema()))
        return root

    async def test_asking_twice_answers_the_second_from_the_memo(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """Criterion 1 through the WM path. Asserted on the walk's node visits, not
        on wall time: a memo hit and a recomputation returning the same rows are
        indistinguishable in the answer."""
        await self._ready(workspace)
        wire = _all_widgets()

        first = result_from_json(await knowledge_service.run_query(workspace, wire))
        first_stats = knowledge_service.last_walk_stats()
        assert first_stats.recomputes == 1

        second = result_from_json(await knowledge_service.run_query(workspace, wire))

        stats = knowledge_service.last_walk_stats()
        assert (stats.hits, stats.recomputes, stats.node_visits) == (1, 0, 1)
        assert second.rows == first.rows

    async def test_invalidating_computes_nothing(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """Criterion 3. The event marks and returns; nothing is read, no rule body
        runs, and the cost is the size of the change rather than the graph."""
        await self._ready(workspace)
        await knowledge_service.run_query(workspace, _all_widgets())
        before = knowledge_service.last_walk_stats().node_visits

        knowledge_service.invalidate([(PROVIDER_ID, "gadget.py")])

        assert knowledge_service.last_walk_stats().node_visits == before

    async def test_a_marked_bucket_whose_facts_did_not_move_does_not_recompute(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """Criterion 4 through the WM, and the case that motivates early cutoff: the
        source changed -- that is what marked the bucket -- and re-reading it
        produced the same fact multiset, so the change stops at the extraction node
        instead of propagating to everything downstream."""
        await self._ready(workspace)
        wire = _all_widgets()
        await knowledge_service.run_query(workspace, wire)

        knowledge_service.invalidate([(PROVIDER_ID, "gadget.py")])
        await knowledge_service.run_query(workspace, wire)

        stats = knowledge_service.last_walk_stats()
        assert (stats.digest_cutoffs, stats.recomputes) == (1, 0)
        assert stats.verified_without_recompute == 1

    async def test_a_bucket_whose_facts_did_move_recomputes(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """The other half: cutoff must not be a way of never noticing anything."""
        await self._ready(workspace)
        wire = _all_widgets()
        await knowledge_service.run_query(workspace, wire)

        store = knowledge_service.load_store(workspace)
        store.ingest(
            PROVIDER_ID,
            [
                *store.bucket(PROVIDER_ID, "gadget.py"),
                FieldFact(
                    entity=Widget.ref(name="sprocket"),
                    field="name",
                    value="sprocket",
                    prov=Provenance(
                        band=Band.DECLARED,
                        provider=PROVIDER_ID,
                        run=RunStamp(id="r-2", observed_at="t"),
                    ),
                ),
            ],
            unit=store.unit(PROVIDER_ID, "gadget.py"),
        )
        knowledge_service.invalidate([(PROVIDER_ID, "gadget.py")])
        answered = result_from_json(await knowledge_service.run_query(workspace, wire))

        stats = knowledge_service.last_walk_stats()
        assert (stats.recomputes, stats.digest_cutoffs) == (1, 0)
        assert sorted(row[1] for row in answered.rows) == ["gadget", "sprocket"]

    async def test_the_cold_start_trigger_and_the_event_trigger_agree(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """Criterion 6 / R12 / D-6. A watcher event and a fingerprint diff reach the
        *same function*, so "correctness never depends on the watcher" is a property
        of there being one path rather than a promise about keeping two in step.

        Here the diff is the real one: the source file is edited, and
        ``invalidate_changed_inputs`` derives the unit list from R11's persisted
        fingerprints without anything having told it what happened."""
        root = await self._ready(workspace)
        wire = _all_widgets()
        await knowledge_service.run_query(workspace, wire)
        (root / "gadget.py").write_text("widget = 2\n")

        knowledge_service.reset()  # a reload: the report has to be recomputed
        knowledge_service.memo().clear()
        await knowledge_service.run_query(workspace, wire)
        by_diff = knowledge_service.invalidate_changed_inputs(workspace)

        assert by_diff == knowledge_service.revision()
        assert knowledge_service.memo().dirty == {(PROVIDER_ID, "gadget.py")}

    async def test_an_untracked_bucket_is_not_treated_as_a_change(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """``UNTRACKED`` says *we could not check*, not *it changed*. Treating it as
        a change would advance the revision on every walk and disable the memo, for
        buckets that may well be current (R9)."""
        root = workspace.ws_dirs_paths[0]
        source = root / "gadget.py"
        source.write_text("widget = 1\n")
        # A unit that declares an input it cannot fingerprint.
        schema = _schema()
        store = FactStore(schema)
        store.ingest(
            PROVIDER_ID,
            [
                FieldFact(
                    entity=Widget.ref(name="gadget"),
                    field="name",
                    value="gadget",
                    prov=Provenance(
                        band=Band.DECLARED,
                        provider=PROVIDER_ID,
                        run=RunStamp(id="r", observed_at="t"),
                    ),
                )
            ],
            unit=Unit(PROVIDER_ID, "gadget.py", untracked=("resolved config",)),
        )
        write_facts(store, root / knowledge_service.DEFAULT_FACTS_PATH)
        await knowledge_service.register_schema(registry_to_json(schema))
        before = knowledge_service.revision()

        knowledge_service.invalidate_changed_inputs(workspace)

        assert knowledge_service.revision() == before
        assert knowledge_service.memo().dirty == frozenset()

    async def test_reloading_the_store_marks_every_bucket_it_held(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """A reload is a change of *unknown* extent, so the blunt instrument is the
        right one: a memo that kept confirming nodes across it would be serving
        answers over facts it never saw."""
        await self._ready(workspace)
        await knowledge_service.run_query(workspace, _all_widgets())
        before = knowledge_service.revision()

        knowledge_service.reset()

        assert knowledge_service.revision() == before + 1
        assert knowledge_service.memo().dirty == {(PROVIDER_ID, "gadget.py")}

    async def test_a_memoized_answer_still_names_the_facts_it_rests_on(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """R15 / criterion 8 through the WM: the node keeps its footprint and its
        attributed buckets across a hit, which is exactly where step 1 makes it easy
        to lose."""
        await self._ready(workspace)
        wire = _all_widgets()
        await knowledge_service.run_query(workspace, wire)
        await knowledge_service.run_query(workspace, wire)
        assert knowledge_service.last_walk_stats().hits == 1

        nodes = list(knowledge_service.memo())

        (query_node,) = [n for n in nodes if n.kind.value == "query"]
        assert query_node.footprint
        assert query_node.depends_on == (("extraction", PROVIDER_ID, "gadget.py"),)

    async def _add_a_widget(self, workspace: context.WorkspaceContext) -> None:
        """Move the bucket's facts, so a cached and a verified read disagree."""
        store = knowledge_service.load_store(workspace)
        store.ingest(
            PROVIDER_ID,
            [
                *store.bucket(PROVIDER_ID, "gadget.py"),
                FieldFact(
                    entity=Widget.ref(name="sprocket"),
                    field="name",
                    value="sprocket",
                    prov=Provenance(
                        band=Band.DECLARED,
                        provider=PROVIDER_ID,
                        run=RunStamp(id="r-2", observed_at="t"),
                    ),
                ),
            ],
            unit=store.unit(PROVIDER_ID, "gadget.py"),
        )
        knowledge_service.invalidate([(PROVIDER_ID, "gadget.py")])

    async def test_a_cached_read_serves_the_memo_and_a_verified_read_does_not(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """**Criterion 9** through the WM, which is the only place it is reachable:
        the memo is in-memory (D-4), so a one-shot CLI run never has a value to
        serve and ``Mode.CACHED`` means something only in a live WM -- exactly where
        §4.12 and G7 say the latency matters.

        Both modes read one node, so what the cached read hands back is what the
        verified pass before it computed."""
        await self._ready(workspace)
        wire = _all_widgets()
        await knowledge_service.run_query(workspace, wire)
        await self._add_a_widget(workspace)

        cached = result_from_json(
            await knowledge_service.run_query(workspace, wire, mode="cached")
        )
        stats = knowledge_service.last_walk_stats()

        assert (stats.cached_serves, stats.recomputes, stats.node_visits) == (1, 0, 1)
        assert [row[1] for row in cached.rows] == ["gadget"], "the pre-change answer"
        assert [r.kind.value for r in cached.freshness.reservations].count(
            "cached"
        ) == 1

        verified = result_from_json(await knowledge_service.run_query(workspace, wire))

        assert knowledge_service.last_walk_stats().recomputes == 1
        assert sorted(row[1] for row in verified.rows) == ["gadget", "sprocket"]
        assert all(r.kind.value != "cached" for r in verified.freshness.reservations)


class TestNoticingTheWorldMoved:
    """On-demand-extraction-plan Phase 1: before this, the store and its verdict
    froze at the first query for the rest of the process's life -- an
    out-of-band ``extract_knowledge`` never reached a running WM at all."""

    async def test_a_verified_query_sees_an_out_of_band_extraction_with_no_restart(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """The scenario the whole phase exists for: a user runs `extract_knowledge`
        from a terminal while their IDE's WM is still up. Without this, the WM
        answers from whatever it read at its first query, forever."""
        root = _populated(workspace)
        await knowledge_service.register_schema(registry_to_json(_schema()))
        wire = _all_widgets()
        first = result_from_json(await knowledge_service.run_query(workspace, wire))
        assert [row[1] for row in first.rows] == ["gadget"]

        _write_fact_file_as(root, root / "gadget.py", "widget-v2")

        second = result_from_json(await knowledge_service.run_query(workspace, wire))

        assert [row[1] for row in second.rows] == ["widget-v2"]

    async def test_a_cached_read_is_unaffected_by_the_fact_file_moving(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """``Mode.CACHED``'s contract is "answer now, tell me it was not
        re-verified" (ADR-0014 D6) -- reloading the store here would upgrade
        that promise into one the mode was never meant to make."""
        root = _populated(workspace)
        await knowledge_service.register_schema(registry_to_json(_schema()))
        wire = _all_widgets()
        await knowledge_service.run_query(workspace, wire)

        _write_fact_file_as(root, root / "gadget.py", "widget-v2")

        cached = result_from_json(
            await knowledge_service.run_query(workspace, wire, mode="cached")
        )

        assert [row[1] for row in cached.rows] == ["gadget"], "the pre-move answer"

    async def test_an_edited_tracked_source_is_stale_on_the_next_verified_query(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """R11's walk survives the trip through a live WM: a query is the first
        thing to run after the edit, with no explicit ``reset()`` or
        ``invalidate()`` in between -- nothing but the query itself notices."""
        root = _populated(workspace)
        await knowledge_service.register_schema(registry_to_json(_schema()))
        (root / "gadget.py").write_text("widget = 2\n")

        result = result_from_json(
            await knowledge_service.run_query(workspace, _all_widgets())
        )

        assert [r.kind.value for r in result.freshness.reservations] == ["stale"]

    async def test_a_cached_read_does_not_re_walk_after_its_source_changes(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """The other half of D-6's choice: ``Mode.CACHED`` must not pay for a
        confirmation walk it did not ask for. Asserted on the report object
        identity, because a walk that happened to reconfirm the same verdict
        would look identical from the rows alone."""
        root = _populated(workspace)
        await knowledge_service.register_schema(registry_to_json(_schema()))
        wire = _all_widgets()
        await knowledge_service.run_query(workspace, wire)
        warm_report = knowledge_service.verify(workspace)

        (root / "gadget.py").write_text("widget = 2\n")
        cached = result_from_json(
            await knowledge_service.run_query(workspace, wire, mode="cached")
        )

        assert knowledge_service.verify(workspace) is warm_report
        assert all(r.kind.value != "stale" for r in cached.freshness.reservations)


class TestTheRunnerReachesThisService:
    def test_the_bridge_is_filled_by_importing_the_service(self) -> None:
        """The runner cannot import a service (services sit above it), so it holds a
        slot the service fills. If nothing filled it, every ``knowledge/query`` would
        answer with a method error and no test above would notice."""
        from finecode.wm_server.runner import knowledge_bridge

        assert knowledge_bridge.handlers() is not None

    async def test_the_bridge_routes_to_this_module(
        self, workspace: context.WorkspaceContext
    ) -> None:
        from finecode.wm_server.runner import knowledge_bridge

        _populated(workspace)
        handlers = knowledge_bridge.handlers()
        assert handlers is not None

        await handlers.register_schema(registry_to_json(_schema()))
        answered = await handlers.run_query(
            workspace, _all_widgets(), mode="verified", limit=None
        )

        assert [row[1]["v"] for row in answered["rows"]] == ["gadget"]

    async def test_the_bridge_routes_record_reads_too(
        self, workspace: context.WorkspaceContext
    ) -> None:
        """A second method on the same slot. Left unrouted, ``knowledge/records``
        would answer with a method error and every test above would still pass --
        the read simply would not be reachable from the side that needs it."""
        from finecode.wm_server.runner import knowledge_bridge

        _populated(workspace)
        handlers = knowledge_bridge.handlers()
        assert handlers is not None

        await handlers.register_schema(registry_to_json(_schema()))
        answered = await handlers.fetch_records(
            workspace, refs_to_json([Widget.ref(name="gadget")])
        )

        (record,) = records_from_json(answered)
        assert [v.value for v in record.fields.values()] == ["gadget"]
