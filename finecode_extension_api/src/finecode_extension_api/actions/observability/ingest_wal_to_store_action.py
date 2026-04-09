# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.resource_uri import ResourceUri


@dataclasses.dataclass(frozen=True)
class WalSourceSpec:
    source_id: str
    format: str
    location_uri: ResourceUri
    include_glob: str | None = None
    exclude_glob: str | None = None
    field_mapping: dict[str, str] | None = None


@dataclasses.dataclass
class IngestWalToStoreRunPayload(code_action.RunActionPayload):
    source_specs: list[WalSourceSpec] | None = None
    """WAL sources to ingest.
    None = discover from project env WAL directories (requires a discovery handler).
    Empty list = explicit no-op (nothing to ingest)."""
    since_ts_iso: str | None = None
    store_uri: ResourceUri | None = None


class IngestWalToStoreRunContext(
    code_action.RunActionContext[IngestWalToStoreRunPayload]
):
    def __init__(
        self,
        run_id: int,
        initial_payload: IngestWalToStoreRunPayload,
        meta: code_action.RunActionMeta,
        info_provider: code_action.RunContextInfoProvider,
        progress_sender: code_action.ProgressSender = code_action._NOOP_PROGRESS_SENDER,
    ) -> None:
        super().__init__(
            run_id=run_id,
            initial_payload=initial_payload,
            meta=meta,
            info_provider=info_provider,
            progress_sender=progress_sender,
        )
        self.source_specs: list[WalSourceSpec] | None = None

    async def init(self) -> None:
        if self.initial_payload.source_specs is not None:
            self.source_specs = list(self.initial_payload.source_specs)


@dataclasses.dataclass
class SourceIngestSummary:
    source_id: str
    files_scanned: int = 0
    events_read: int = 0
    events_inserted: int = 0
    events_skipped_duplicate: int = 0
    events_failed_parse: int = 0


@dataclasses.dataclass
class IngestWalToStoreRunResult(code_action.RunActionResult):
    schema_version: int = 0
    source_summary: list[SourceIngestSummary] = dataclasses.field(default_factory=list)
    events_ingested: int = 0
    events_skipped_duplicate: int = 0
    events_failed_parse: int = 0
    first_event_ts_iso: str | None = None
    last_event_ts_iso: str | None = None
    store_uri: ResourceUri | None = None
    warnings: list[str] = dataclasses.field(default_factory=list)

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, IngestWalToStoreRunResult):
            return

        if other.schema_version:
            self.schema_version = other.schema_version
        if other.store_uri is not None:
            self.store_uri = other.store_uri
        self.events_ingested += other.events_ingested
        self.events_skipped_duplicate += other.events_skipped_duplicate
        self.events_failed_parse += other.events_failed_parse
        self.warnings += other.warnings

        if self.first_event_ts_iso is None or (
            other.first_event_ts_iso is not None
            and other.first_event_ts_iso < self.first_event_ts_iso
        ):
            self.first_event_ts_iso = other.first_event_ts_iso
        if self.last_event_ts_iso is None or (
            other.last_event_ts_iso is not None
            and other.last_event_ts_iso > self.last_event_ts_iso
        ):
            self.last_event_ts_iso = other.last_event_ts_iso

        by_source: dict[str, SourceIngestSummary] = {
            item.source_id: item for item in self.source_summary
        }
        for incoming in other.source_summary:
            existing = by_source.get(incoming.source_id)
            if existing is None:
                by_source[incoming.source_id] = dataclasses.replace(incoming)
                continue
            existing.files_scanned += incoming.files_scanned
            existing.events_read += incoming.events_read
            existing.events_inserted += incoming.events_inserted
            existing.events_skipped_duplicate += incoming.events_skipped_duplicate
            existing.events_failed_parse += incoming.events_failed_parse

        self.source_summary = list(by_source.values())

    def to_text(self) -> str | textstyler.StyledText:
        lines = [
            f"Ingested events: {self.events_ingested}",
            f"Skipped duplicates: {self.events_skipped_duplicate}",
            f"Failed parse: {self.events_failed_parse}",
        ]
        if self.store_uri is not None:
            lines.append(f"Store: {self.store_uri}")
        if self.first_event_ts_iso is not None:
            lines.append(f"First event ts: {self.first_event_ts_iso}")
        if self.last_event_ts_iso is not None:
            lines.append(f"Last event ts: {self.last_event_ts_iso}")

        for summary in self.source_summary:
            lines.append(
                "Source "
                f"{summary.source_id}: files={summary.files_scanned}, "
                f"read={summary.events_read}, inserted={summary.events_inserted}, "
                f"duplicates={summary.events_skipped_duplicate}, failed={summary.events_failed_parse}"
            )

        if len(self.warnings) > 0:
            lines.append("Warnings:")
            lines.extend(f"- {item}" for item in self.warnings)

        return "\n".join(lines)


class IngestWalToStoreAction(
    code_action.Action[
        IngestWalToStoreRunPayload,
        IngestWalToStoreRunContext,
        IngestWalToStoreRunResult,
    ]
):
    """Ingest write-ahead-log events from generic sources into a durable store."""

    PAYLOAD_TYPE = IngestWalToStoreRunPayload
    RUN_CONTEXT_TYPE = IngestWalToStoreRunContext
    RESULT_TYPE = IngestWalToStoreRunResult
