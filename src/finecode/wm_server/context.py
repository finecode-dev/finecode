from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from finecode.wm_server import domain
from finecode.wm_server.runner.runner_client import ExtensionRunnerInfo

if TYPE_CHECKING:
    from finecode_jsonrpc._io_thread import AsyncIOThread
    from finecode.wm_server.wal import WalWriter


@dataclass
class WorkspaceContext:
    # ws directories paths - expected to be workspace root and other directories in
    # workspace if they are outside of workspace root
    ws_dirs_paths: list[Path]
    # all projects in the workspace
    ws_projects: dict[Path, domain.Project] = field(default_factory=dict)
    # <project_path:config>
    ws_projects_raw_configs: dict[Path, dict[str, Any]] = field(default_factory=dict)
    # <project_path:<env_name:runner_info>>
    ws_projects_extension_runners: dict[Path, dict[str, ExtensionRunnerInfo]] = field(
        default_factory=dict
    )
    runner_io_thread: AsyncIOThread | None = None

    # LSP doesn't provide endpoint to get opened files on client. The server should
    # listen to didOpen and didClose events and manage state by itself. In this
    # dictionary meta info of opened document is stored to be able to provide opened files
    # to ERs in case of their restart.
    # TODO: move in LSP server
    opened_documents: dict[str, domain.TextDocumentInfo] = field(default_factory=dict)

    # Handler config overrides from CLI env vars or options.
    # Format: {action_name: {handler_name_or_empty: {param: value}}}
    # Empty string key "" means action-level (applies to all handlers).
    handler_config_overrides: dict[str, dict[str, dict[str, str]]] = field(
        default_factory=dict
    )

    # cache
    # <directory: <action_name: project_path>>
    project_path_by_dir_and_action: dict[str, dict[str, Path]] = field(
        default_factory=dict
    )
    cached_actions_by_id: dict[str, CachedAction] = field(default_factory=dict)
    # payload schema cache: project_path → {action_name: JSON Schema fragment | None}
    ws_action_schemas: dict[Path, dict[str, dict | None]] = field(default_factory=dict)
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


@dataclass
class CachedAction:
    action_id: str
    project_path: Path
    action_name: str
