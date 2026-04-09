# fine_wal_explorer

WAL ingestion, discovery, and exploration handlers for FineCode.

This extension provides a DuckDB-backed WAL Explorer workflow:

- discover WAL sources from project environments
- ingest WAL JSONL events into a DuckDB store
- serve a local dashboard and HTTP API from that store

It works with explicit `WalSourceSpec` inputs and also supports automatic discovery for the standard FineCode WAL directories inside project environments.

## Included handlers

- `DiscoverWalSourcesActionHandler`: Handles `discover_wal_sources` action by scanning `.venvs/<env>/state/finecode/wal/{wm,er}/` for each env from `dependency-groups`.
- `IngestWalSourceDiscoveryHandler`: Discovery bridge for `ingest_wal_to_store` that calls `discover_wal_sources` when `source_specs` is not explicitly provided.
- `IngestWalToStoreHandler`: Ingests JSONL WAL events into a DuckDB store, records ingest runs, and skips duplicates when the same event is seen again.
- `ServeWalExplorerFromStoreHandler`: Serves the local WAL Explorer dashboard and JSON endpoints backed by the DuckDB store.

In the default preset, ingest runs as a sequential chain:

1. `IngestWalSourceDiscoveryHandler`
2. `IngestWalToStoreHandler`

## Source format

Sources are provided as `WalSourceSpec` values with:

- `location_uri` pointing to a WAL file or directory
- optional `include_glob` and `exclude_glob` filters
- optional `field_mapping` overrides when the source uses different field names

By default the handlers expect JSONL event records with fields like `ts`, `event_type`, `wal_run_id`, `action_name`, and `payload`.

## Store-backed explorer

When `store_uri` is omitted, ingestion uses the active environment state directory at:

`<venv>/state/finecode/wal_explorer/store.duckdb`

The served explorer exposes:

- `/` dashboard
- `/runs` and `/runs.html`
- `/timeline`
- `/events` and `/events.html`
- `/metrics`
- `/health` and `/health.html`
- `/ingest` for refresh ingests triggered by the UI or other local clients
