from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from loguru import logger

from finecode.wm_server import domain
from finecode.wm_server.runner.runner_client import ExtensionRunnerInfo
from finecode.wm_server.services import process_budget

if TYPE_CHECKING:
    from finecode_jsonrpc._io_thread import AsyncIOThread

    from finecode.wm_server.wal import WalWriter


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
       ``otlp_endpoint``, ``handler_config_overrides``, ``service_config_overrides``
       are set from config and are immutable thereafter.  All collection and
       cache fields start empty.  Both locks are created and ready.

    2. **Runner IO thread** — ``runner_io_thread`` is set once during WM startup
       (before any runner is started).  It is ``None`` before that point and
       non-``None`` for the rest of the server's lifetime.

    3. **Workspace discovery** — triggered by ``addDir`` API calls.
       ``ws_dirs_paths`` grows; ``ws_projects`` and ``ws_workspace_packages`` are
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
        call through the same chokepoint. See ADR-0063.  Its size is one half
        of the combined subprocess-concurrency budget (ADR-0093).

    ``process_budget``
        The one machine-wide budget of subprocess work slots, leased to ERs
        per action run and reclaimed on run end or ER death.  Each ER's
        leased quota sizes that ER's local ``ProcessSlots`` gate, which both
        ``CommandRunner`` and ``ProcessExecutor`` draw from. See ADR-0090.
        Its size is the other half of the same combined budget (ADR-0093).

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

    # Name → absolute path of workspace-resident packages.
    # Populated from finecode-workspace.toml during workspace scan; stable after that.
    ws_workspace_packages: dict[str, Path] = field(default_factory=dict)

    # Package name → wheel built from that package's source for the active
    # wheel install mode. Empty in editable mode; populated by prepare-envs
    # after the wheelhouse fan-out.
    ws_workspace_package_wheels: dict[str, Path] = field(default_factory=dict)

    # How workspace packages are installed in envs ("editable" or "wheel").
    # Resolved from [workspace.workspace_packages_install] per dev-env (or the
    # CLI flag override); "editable" until a prepare-envs run resolves it.
    workspace_packages_install_mode: Literal["editable", "wheel"] = "editable"

    # Packages the wheelhouse must skip; kept editable in every env even in
    # wheel mode. Resolved alongside the install mode by prepare-envs.
    ws_workspace_packages_install_exclude: set[str] = field(default_factory=set)

    # Canonical package name → selected extras, read from the gitignored
    # finecode-workspace-user.toml. Lazily computed by
    # read_configs.read_workspace_extra_selection; reset on config reload so a
    # rename/removal of the selection file is observed.
    ws_extra_selection: dict[str, list[str]] = field(default_factory=dict)

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

    # Service config overrides supplied via CLI-detected environment variables.
    # Format: {service_name: {nested param path as a dict}}
    # Keyed by ServiceDeclaration.name (the addressing alias), not interface.
    # Set from config at construction; immutable thereafter.
    service_config_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)

    # project_path → { run_id → InFlightRun }: runs dispatched and not yet
    # finished.  Always maintained, independently of whether the WAL is enabled,
    # because recovery consults it to decide whether replacing that project's
    # runners would kill a run (ADR-0079).  Keyed by run id rather than counted,
    # since run fan-out is re-entrant and a project can hold several at once.
    in_flight_runs: dict[Path, dict[str, domain.InFlightRun]] = field(
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

    # Both budgets below are sized from ONE combined machine budget in
    # __post_init__ (ADR-0093): their sum stays at or below
    # machine_subprocess_budget(), so the WM's event loop keeps a free core.
    # They stay separate objects on purpose — a run holding work slots must
    # never block on the startup cap to start the ER it fans into (ADR-0090).

    # Bounds how many ERs may be mid-startup (spawned through RPC-connected) at
    # once, across every trigger (workspace init, matrixed run, prepare-envs'
    # runner-start step) — see ADR-0063. Sized from the combined budget.
    er_startup_semaphore: asyncio.Semaphore = field(init=False)

    # The machine-wide budget of subprocess work slots, leased to ERs per action
    # run and reclaimed on run end or ER death (ADR-0090).  Sized from the same
    # combined budget as er_startup_semaphore.
    process_budget: process_budget.ProcessBudget = field(init=False)

    def workspace_packages_wire(self) -> dict[str, dict]:
        """Project the resolved workspace packages into the WM API/ER wire shape.

        Each entry carries the package's source directory and the resolved
        install decision for the active mode. ``editable`` is True for editable
        mode and for an excluded package; otherwise the package installs from
        ``wheel``, which is None when the wheelhouse has no entry — the
        consumer turns that into the P5/R4 error rather than falling back to
        editable.
        """
        result: dict[str, dict] = {}
        for name, package_dir in self.ws_workspace_packages.items():
            editable = (
                self.workspace_packages_install_mode == "editable"
                or name in self.ws_workspace_packages_install_exclude
            )
            wheel = None if editable else self.ws_workspace_package_wheels.get(name)
            result[name] = {
                "dir": package_dir.as_posix(),
                "wheel": wheel.as_posix() if wheel is not None else None,
                "editable": editable,
            }
        return result

    def __post_init__(self) -> None:
        budgets = process_budget.resolve_subprocess_budgets()
        logger.info(
            f"Subprocess concurrency budget: {budgets.total} total "
            f"({budgets.total_source}); ER startup cap {budgets.startup_cap} "
            f"(half of combined budget); process work budget {budgets.work_cap}"
        )
        self.er_startup_semaphore = asyncio.Semaphore(budgets.startup_cap)
        self.process_budget = process_budget.ProcessBudget(budgets.work_cap)


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
