from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from finecode.wm_server import domain
from finecode.wm_server.runner.runner_client import ExtensionRunnerInfo
from finecode_extension_runner.concurrency import (
    ConcurrencyDecision,
    machine_subprocess_budget,
)

if TYPE_CHECKING:
    from finecode_jsonrpc._io_thread import AsyncIOThread
    from finecode.wm_server.wal import WalWriter


def resolve_er_startup_concurrency(env_value: str | None = None) -> ConcurrencyDecision:
    """Effective cap on concurrently-starting Extension Runner processes —
    how many ERs may be mid-startup (spawned, importing, not yet reachable
    over its RPC channel) at once, regardless of what triggered the starts:
    workspace init, a matrixed ``run``, or ``prepare-envs``' own runner-start
    step all funnel through the same chokepoint
    (``runner_manager._start_extension_runner_process``) and share this one
    cap. See ADR-0063.

    Once an ER reports its port and its RPC channel connects, the cap no
    longer applies to it — the triggering action then runs in that ER's own
    process, a separate and much more variable resource cost (e.g. an actual
    test-suite run) that this cap deliberately does not throttle.

    Unlike the two ``prepare-envs`` layers (ADR-0055), this guards a single
    flat resource axis rather than two composing layers, so it uses the full
    ``machine_subprocess_budget()`` rather than its sqrt-split.

    Priority: ``FINECODE_WM_MAX_CONCURRENT_ER_STARTS`` env var (if set) >
    ``machine_subprocess_budget()``. Machine-bound like the layers above, so
    no ``finecode-workspace.toml`` equivalent. Unlike
    ``prepare_envs_service.resolve_project_concurrency``, there is no CLI
    flag: this cap protects
    the WM server's entire lifetime, not one command's request, so it is
    resolved once — here, as the default factory for
    ``WorkspaceContext.er_startup_semaphore`` — when the long-lived
    ``WorkspaceContext`` is constructed, rather than threaded through a
    single RPC call. ``env_value`` is injectable for tests; production
    callers omit it and let this read ``os.environ`` directly.
    """
    if env_value is None:
        env_value = os.environ.get("FINECODE_WM_MAX_CONCURRENT_ER_STARTS")
    if env_value is not None:
        return ConcurrencyDecision(
            max(int(env_value), 1), "FINECODE_WM_MAX_CONCURRENT_ER_STARTS env var"
        )
    return ConcurrencyDecision(
        machine_subprocess_budget(),
        f"computed default (machine budget {machine_subprocess_budget()})",
    )


def _make_er_startup_semaphore() -> asyncio.Semaphore:
    decision = resolve_er_startup_concurrency()
    logger.info(
        f"ER startup concurrency cap: {decision.value} ({decision.source})"
    )
    return asyncio.Semaphore(decision.value)


@dataclass
class WorkspaceContext:
    """Shared mutable state of the WM server.

    A single ``WorkspaceContext`` is created at server startup and passed to
    every service and API handler.  It is the authoritative source of truth for
    the workspace's current state.

    Initialization lifecycle
    ------------------------
    Fields are populated in stages as the server starts up and clients connect:

    1. **Construction** — ``ws_dirs_paths`` is set (may be empty ``[]`` initially).
       ``otlp_endpoint``, ``handler_config_overrides`` are set from config and
       are immutable thereafter.  All collection and cache fields start empty.
       Both locks are created and ready.

    2. **Runner IO thread** — ``runner_io_thread`` is set once during WM startup
       (before any runner is started).  It is ``None`` before that point and
       non-``None`` for the rest of the server's lifetime.

    3. **Workspace discovery** — triggered by ``addDir`` API calls.
       ``ws_dirs_paths`` grows; ``ws_projects`` and ``ws_editable_packages`` are
       populated.  Protected by ``workspace_state_lock``.

    4. **Project initialization** — per project, protected by the project's entry
       in ``project_init_locks``.  ``ws_projects_raw_configs`` is populated, then
       the project entry in ``ws_projects`` transitions through
       ``Project → CollectedProject → ResolvedProject``.

    5. **Runner startup** — ``ws_projects_extension_runners`` entries are created
       as ERs start.  Caches are populated lazily on first use.

    6. **WAL** — ``wal_writer`` is set during startup if WAL is configured.
       ``None`` means WAL is disabled.

    Concurrency model
    -----------------
    ``workspace_state_lock``
        Serializes the *fast* phase of workspace mutations: directory-list
        updates, filesystem scan, and ``projects_to_init`` computation.
        Released *before* the slow runner-startup phase.  Always acquire this
        before reading or writing ``ws_dirs_paths`` or ``ws_projects``.

    ``project_init_locks[path]``
        One lock per project path.  Guards the slow per-project work: config
        reading, preset resolution, and runner startup.  Entries are created
        inside ``workspace_state_lock``, so a key is always present before it
        is awaited.  A *held* lock means initialization is in progress for that
        project.

    ``er_startup_semaphore``
        Bounds concurrent ER *startups* only — held from just before an ER
        process is spawned until its RPC channel is confirmed connected, then
        released.  Does not bound the triggering action's execution, which
        runs afterward in that ER's own process.  Shared by every start
        trigger (workspace init, matrixed run, prepare-envs), since they all
        call through the same chokepoint. See ADR-0063.

    Caches
    ------
    ``project_path_by_dir_and_action``, ``cached_actions_by_id``, and
    ``ws_action_schemas`` are populated lazily and must be invalidated when the
    projects they reference change.  They carry no correctness guarantees beyond
    the point of the last invalidation.
    """

    # Set at construction; grows via addDir API calls.
    ws_dirs_paths: list[Path]

    # All projects discovered in the workspace.  Values transition through
    # Project → CollectedProject → ResolvedProject as initialization progresses.
    # Mutated under workspace_state_lock (discovery) and project_init_locks (init).
    ws_projects: dict[Path, domain.Project] = field(default_factory=dict)

    # Name → absolute path of workspace-resident editable packages.
    # Populated from finecode-workspace.toml during workspace scan; stable after that.
    ws_editable_packages: dict[str, Path] = field(default_factory=dict)

    # Raw definition-file config per project path.  Populated by read_project_config
    # before collect_project is called.  Entries are not automatically removed when
    # projects are re-initialized.
    ws_projects_raw_configs: dict[Path, dict[str, Any]] = field(default_factory=dict)

    # project_path → { env_name → ExtensionRunnerInfo }.
    # Entries are added when an ER is started; updated in-place as its status changes.
    ws_projects_extension_runners: dict[Path, dict[str, ExtensionRunnerInfo]] = field(
        default_factory=dict
    )

    # Set once during WM startup, before any runner is started.
    # None only before startup completes; non-None for the server's full lifetime.
    runner_io_thread: AsyncIOThread | None = None

    # OTLP endpoint for telemetry.  Set from config at construction; None if
    # telemetry is not configured.  Immutable after construction.
    otlp_endpoint: str | None = None

    # In-memory state of documents opened by the client.  Populated by
    # didOpen / didChange notifications; cleared by didClose.  Kept here so
    # that restarted ERs can be re-supplied with open-document content.
    # TODO: move to LSP server — this is an LSP concern, not a WM concern.
    opened_documents: dict[str, domain.TextDocumentInfo] = field(default_factory=dict)

    # Handler config overrides supplied via CLI flags or environment variables.
    # Format: {action_name: {handler_name_or_"": {param: value}}}
    # The empty-string key "" means the override applies to all handlers of the action.
    # Set from config at construction; immutable thereafter.
    handler_config_overrides: dict[str, dict[str, dict[str, str]]] = field(
        default_factory=dict
    )

    # --- Caches (lazily populated; must be invalidated on project changes) -------

    # directory path (str) → { action_name → project_path }
    project_path_by_dir_and_action: dict[str, dict[str, Path]] = field(
        default_factory=dict
    )

    # action node ID ("project_path::action_source") → CachedAction
    cached_actions_by_id: dict[str, CachedAction] = field(default_factory=dict)

    # project_path → { action_name → JSON Schema fragment | None }
    ws_action_schemas: dict[Path, dict[str, dict | None]] = field(default_factory=dict)

    # --- Infrastructure ---------------------------------------------------------

    # WAL writer.  Set during startup if WAL is configured; None means WAL is
    # disabled for this run.
    wal_writer: WalWriter | None = None

    # Serializes the fast discovery phase of workspace mutations (addDir, removeDir,
    # startRunners): dir-list updates, filesystem scan, and projects_to_init
    # computation.  Released before the slow runner-startup phase begins.
    workspace_state_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # Per-project initialization locks.  Guard the slow per-project work: config
    # reading, preset resolution, and runner startup.  Created once per project path
    # inside workspace_state_lock so they are always present before being awaited.
    # A locked entry means initialization is in progress for that project.
    project_init_locks: dict[Path, asyncio.Lock] = field(default_factory=dict)

    # Locks used by install_env_for_project to serialize concurrent root-runner
    # startups during env installation.  Intentionally separate from
    # project_init_locks: the handlers (_handle_add_dir, _handle_start_runners)
    # hold project_init_locks for the entire slow Phase 2, so reusing them here
    # would deadlock when _auto_prepare_and_retry calls install_env_for_project
    # from inside that same Phase 2.
    env_install_locks: dict[Path, asyncio.Lock] = field(default_factory=dict)

    # Bounds how many ERs may be mid-startup (spawned through RPC-connected) at
    # once, across every trigger (workspace init, matrixed run, prepare-envs'
    # runner-start step) — see resolve_er_startup_concurrency and ADR-0063.
    # Sized once here, at WorkspaceContext construction, since the WM
    # constructs exactly one WorkspaceContext for its whole process lifetime.
    er_startup_semaphore: asyncio.Semaphore = field(
        default_factory=_make_er_startup_semaphore
    )


@dataclass
class CachedAction:
    action_id: str
    project_path: Path
    action_source: str


def pick_workspace_root_dir(ws_context: WorkspaceContext) -> Path | None:
    """Return the workspace root directory.

    Single ws dir → return it directly.
    Multiple ws dirs → return the one containing finecode-workspace.toml.
    Returns None when multiple dirs exist and none has finecode-workspace.toml.
    """
    if len(ws_context.ws_dirs_paths) == 1:
        return ws_context.ws_dirs_paths[0]
    for ws_dir in ws_context.ws_dirs_paths:
        if (ws_dir / "finecode-workspace.toml").exists():
            return ws_dir
    return None
