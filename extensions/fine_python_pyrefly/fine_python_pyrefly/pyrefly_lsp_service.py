from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import importlib.metadata
import sys
import time
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from fine_inspect_code.diagnostic_types import map_lsp_diagnostics
from fine_lint.diagnostic_types import Diagnostic
from fine_semantic_tokens.text_document_semantic_tokens_action import (
    SEMANTIC_TOKEN_MODIFIERS,
    SEMANTIC_TOKEN_TYPES,
)
from finecode_extension_api import code_action, service
from finecode_extension_api.contrib._loop_event import RearmableLoopEvent
from finecode_extension_api.contrib.lsp_service import LspService
from finecode_extension_api.interfaces import (
    iextensionrunnerinfoprovider,
    ifileeditor,
    ilogger,
    ilspclient,
    iprojectinfoprovider,
    iworkslots,
    iworkspaceactionrunner,
    iworkspaceinfoprovider,
)
from finecode_extension_api.resource_uri import ResourceUri, resource_uri_to_path

from fine_python_pyrefly.announce_pyrefly_project_action import (
    AnnouncePyreflyProjectAction,
)
from fine_python_pyrefly.check_files_on_pyrefly_host_action import (
    CheckFilesOnPyreflyHostAction,
)
from fine_python_pyrefly.host_types import (
    AnnouncePyreflyProjectPayload,
    CheckFilesOnPyreflyHostPayload,
    FileCheckOutcome,
    PyreflyFolderSettings,
    PyreflyItemQuery,
    PyreflyPositionQuery,
    PyreflyPositionQueryKind,
    PyreflyProjectIdentity,
    PyreflyQueryKind,
    PyreflyRangeQuery,
    PyreflyTopology,
    QueryPyreflyHostPayload,
)
from fine_python_pyrefly.pyrefly_config import PyreflyConfig
from fine_python_pyrefly.query_pyrefly_host_action import QueryPyreflyHostAction

_PYREFLY_CLIENT_CAPABILITIES: dict[str, Any] = {
    "textDocument": {
        "synchronization": {
            "dynamicRegistration": False,
            "didSave": True,
        },
        "hover": {
            "dynamicRegistration": False,
            "contentFormat": ["markdown", "plaintext"],
        },
        "publishDiagnostics": {"relatedInformation": True},
        "semanticTokens": {
            "dynamicRegistration": False,
            "tokenTypes": SEMANTIC_TOKEN_TYPES,
            "tokenModifiers": SEMANTIC_TOKEN_MODIFIERS,
            "formats": ["relative"],
            "requests": {"full": True, "range": True},
            "multilineTokenSupport": False,
            "overlappingTokenSupport": False,
        },
        "inlayHint": {"dynamicRegistration": False},
        "definition": {"dynamicRegistration": False, "linkSupport": False},
        "references": {"dynamicRegistration": False},
        "typeDefinition": {"dynamicRegistration": False, "linkSupport": False},
        "implementation": {"dynamicRegistration": False, "linkSupport": False},
        "documentHighlight": {"dynamicRegistration": False},
        "callHierarchy": {
            "dynamicRegistration": False,
        },
        "typeHierarchy": {
            "dynamicRegistration": False,
        },
    },
    "workspace": {
        "workspaceFolders": True,
        "configuration": True,
        "didChangeWatchedFiles": {
            "dynamicRegistration": True,
            "relativePatternSupport": False,
        },
    },
}

# The transport runs on the WM's behalf, not on a user's: it exists to carry an
# IDE request to the host, so SYSTEM/IDE is the accurate description.
_FORWARD_META = code_action.RunActionMeta(
    trigger=code_action.RunActionTrigger.SYSTEM,
    dev_env=code_action.DevEnv.IDE,
)
_FOLDER_PULL_TIMEOUT_SEC = 5.0
_RECHECK_BARRIER_SEC = 2.0
_IDLE_SHUTDOWN_SEC = 15 * 60.0
_IDLE_SCAN_SEC = 60.0
_HOST_ACTION_SOURCE = "fine_python_pyrefly.AnnouncePyreflyProjectAction"


class PyreflyLspService(service.DisposableService):
    """Pyrefly LSP service — per-project server, workspace host, or forwarding facade.

    ``topology = "per-project"`` (the default) is a thin wrapper around one
    ``LspService`` per project. ``topology = "workspace"`` puts one server in the
    workspace-root project and forwards every other project's requests to it as
    ordinary project-scoped actions (ADR-0108).
    """

    def __init__(
        self,
        lsp_client: ilspclient.ILspClient,
        file_editor: ifileeditor.IFileEditor,
        logger: ilogger.ILogger,
        extension_runner_info_provider: iextensionrunnerinfoprovider.IExtensionRunnerInfoProvider,
        work_slots: iworkslots.IWorkSlots,
        pyrefly_config: PyreflyConfig,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        workspace_info_provider: iworkspaceinfoprovider.IWorkspaceInfoProvider,
        workspace_action_runner: iworkspaceactionrunner.IWorkspaceActionRunner,
    ) -> None:
        self._lsp_client = lsp_client
        self._file_editor = file_editor
        self._logger = logger
        self._extension_runner_info_provider = extension_runner_info_provider
        self._work_slots = work_slots
        self._pyrefly_config = pyrefly_config
        self._project_info_provider = project_info_provider
        self._workspace_info_provider = workspace_info_provider
        self._workspace_action_runner = workspace_action_runner

        self._topology = PyreflyTopology(pyrefly_config.topology)
        self._project_dir = project_info_provider.get_current_project_dir_path()
        self._root_dir: Path | None = None
        # Set by `init`: a non-root workspace project forwards, the root hosts.
        self._forwards = False
        self._is_host = False
        self._preconditions_checked = False

        # Built in `init`, once the topology is known: a forwarding facade must
        # not lease work slots for a server it will never start (F9).
        self._lsp_service: LspService | None = None
        self._pyrefly_settings: dict[str, Any] = {}
        self._python_path: str = ""
        self._settings_version = 0
        self._sent_settings_version: int | None = None

        # pyrefly's own environment/interpreter auto-detection does not know about
        # FineCode's per-project envs, so without this it resolves imports against the
        # wrong (or no) site-packages. Applied here so every feature (hover, definition,
        # inlay hints, ...) gets it, not just type checking.
        #
        # A single LSP server can only be configured with one resolution env at startup
        # (it cannot switch per file), so the broadest env is used: "dev" is a superset
        # of "runtime" (project deps + dev-only deps such as pytest), which lets test
        # files resolve their imports too. Falling back to "runtime" when "dev" does not
        # exist only loses symbols, never adds false ones.
        resolution_env = "dev"
        venv_dir = extension_runner_info_provider.get_venv_dir_path_of_env(
            resolution_env
        )
        if not venv_dir.exists():
            resolution_env = "runtime"
            venv_dir = extension_runner_info_provider.get_venv_dir_path_of_env(
                resolution_env
            )
        logger.debug(f"pyrefly resolves imports against the '{resolution_env}' env")
        interpreter_path = extension_runner_info_provider.get_venv_python_interpreter(
            venv_dir
        )
        site_packages = extension_runner_info_provider.get_venv_site_packages(venv_dir)
        self._python_path = str(interpreter_path)
        self.update_settings(
            {
                "pythonPath": self._python_path,
                "pyrefly": {"extraPaths": [str(p) for p in site_packages]},
            }
        )
        if pyrefly_config.lsp_config_path is not None:
            self.update_settings(
                {"pyrefly": {"configPath": str(pyrefly_config.lsp_config_path)}}
            )

        # Host state. Only used when this project is the workspace root in
        # workspace topology.
        self._host_version = self._read_pyrefly_version()
        self._project_registry: dict[str, PyreflyProjectIdentity] = {}
        self._session_folder_fingerprints: dict[str, str] = {}
        self._folder_pull_events: dict[str, RearmableLoopEvent] = {}
        self._pending_added: dict[str, str] = {}
        self._pending_changed: dict[str, str] = {}
        self._pending_sweeps: set[Path] = set()
        self._pending_waiters: list[asyncio.Event] = []
        self._waiter_errors: dict[asyncio.Event, list[str]] = {}
        self._flush_task: asyncio.Task[None] | None = None
        self._host_lock = asyncio.Lock()
        self._idle_task: asyncio.Task[None] | None = None
        self._last_activity = time.monotonic()

    @override
    async def init(self) -> None:
        root = await self._workspace_info_provider.get_workspace_root()
        self._root_dir = root
        if self._topology == PyreflyTopology.WORKSPACE:
            if root is None:
                raise code_action.ActionFailedException(
                    "pyrefly workspace topology needs one workspace root: keep a"
                    " single workspace directory, or mark it with"
                    " finecode-workspace.toml. Never fall back to one server per"
                    " project."
                )
            if not self._project_dir.is_relative_to(root):
                raise code_action.ActionFailedException(
                    f"Project {self._project_dir} is outside the workspace root"
                    f" {root}. Move it under the root, or set topology ="
                    ' "per-project" for every project.'
                )
            self._forwards = self._project_dir != root
            self._is_host = self._project_dir == root

        self._lsp_service = LspService(
            lsp_client=self._lsp_client,
            file_editor=self._file_editor,
            logger=self._logger,
            work_slots=None if self._forwards else self._work_slots,
            cmd=[str(Path(sys.executable).parent / "pyrefly"), "lsp"],
            language_id="python",
            readable_id="pyrefly-lsp",
            client_capabilities=_PYREFLY_CLIENT_CAPABILITIES,
        )
        self._lsp_service.update_settings(
            {"pythonPath": self._python_path, "pyrefly": dict(self._pyrefly_settings)}
        )
        if self._is_host:
            self._lsp_service.set_configuration_resolver(
                self._resolve_folder_configuration
            )
        await self._lsp_service.init()

        if self._is_host:
            self._idle_task = asyncio.create_task(self._idle_loop())
        if self._forwards:
            await self._announce()

    @override
    async def dispose(self) -> None:
        if self._idle_task is not None:
            _ = self._idle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._idle_task
            self._idle_task = None
        if self._lsp_service is not None:
            await self._lsp_service.dispose()

    def update_settings(self, settings: dict[str, Any]) -> None:
        """Update pyrefly LSP settings.

        The underlying LspService.update_settings merges the top-level dict
        shallowly, which would let a later call overwrite the whole "pyrefly"
        section (e.g. dropping extraPaths when a handler adds displayTypeErrors).
        Deep-merge that one section here instead.
        """
        pyrefly_settings = settings.get("pyrefly")
        if pyrefly_settings is not None:
            self._pyrefly_settings = {**self._pyrefly_settings, **pyrefly_settings}
            settings = {**settings, "pyrefly": self._pyrefly_settings}
        self._settings_version += 1
        if self._lsp_service is not None:
            self._lsp_service.update_settings(settings)

    async def ensure_started(self, root_uri: str) -> None:
        """Start the local server, then push settings that changed after a start.

        A forwarding facade owns no local server: the host holds the session and
        the identity's fingerprint carries any settings change on the next
        forward, so this is a no-op there.

        Pyrefly rereads client settings on ``workspace/didChangeConfiguration``, so
        settings registered after the server started are pushed instead of being lost.
        The version is read before awaiting the start: an update landing while the
        start is in flight belongs to the next call, not to the one already running.
        """
        if self._forwards:
            return
        inner = self._inner()
        version = self._settings_version
        await inner.ensure_started(root_uri)
        if self._sent_settings_version is None:
            self._sent_settings_version = version
            return
        if version != self._sent_settings_version:
            self._sent_settings_version = version
            await inner.send_settings()

    @property
    def forwards_to_host(self) -> bool:
        """Whether this service answers by forwarding to the workspace host."""
        return self._forwards

    @property
    def project_identity(self) -> PyreflyProjectIdentity:
        return self._identity()

    @property
    def server_capabilities(self) -> dict[str, Any]:
        if self._lsp_service is None:
            return {}
        return self._lsp_service.server_capabilities

    async def sync_watched_files(
        self, file_paths: collections.abc.Sequence[Path], recheck_timeout: float
    ) -> set[Path]:
        if self._forwards:
            raise code_action.ActionFailedException(
                "Workspace-topology sweeps run on the host flush, not locally"
            )
        return await self._inner().sync_watched_files(file_paths, recheck_timeout)

    async def check_file(
        self,
        file_path: Path,
        timeout: float = 30.0,
        content: str | None = None,
    ) -> list[Diagnostic]:
        """Check a file locally (host or per-project mode).

        Raises:
            ActionFailedException: this service forwards to a workspace host;
                checks go through ``forward_check_chunk`` there.
        """
        if self._forwards:
            raise code_action.ActionFailedException(
                "Workspace-topology pyrefly checks go through forward_check_chunk,"
                " not check_file"
            )
        raw_diagnostics = await self._inner().check_file(
            file_path, timeout=timeout, content=content
        )
        return map_lsp_diagnostics(raw_diagnostics, default_source="pyrefly")

    async def forward_check_chunk(
        self,
        files: list[ResourceUri],
        contents: dict[str, str],
        *,
        sweep: bool,
    ) -> list[FileCheckOutcome]:
        """Send one chunk of files to the workspace host and return its outcomes."""
        root = await self._forward_root()
        results = await self._workspace_action_runner.run_action_in_projects(
            CheckFilesOnPyreflyHostAction,
            CheckFilesOnPyreflyHostPayload(
                identity=self._identity(),
                files=files,
                sweep=sweep,
                contents=contents,
            ),
            meta=_FORWARD_META,
            project_paths=[root],
        )
        result = results.get(root)
        if result is None:
            raise code_action.ActionFailedException(
                f"The pyrefly host at {root} returned no check result"
            )
        return result.files

    async def _forward_query(
        self, query: PyreflyPositionQuery | PyreflyRangeQuery | PyreflyItemQuery
    ) -> dict[str, Any] | list[Any] | None:
        root = await self._forward_root()
        results = await self._workspace_action_runner.run_action_in_projects(
            QueryPyreflyHostAction,
            QueryPyreflyHostPayload(identity=self._identity(), query=query),
            meta=_FORWARD_META,
            project_paths=[root],
        )
        result = results.get(root)
        if result is None:
            raise code_action.ActionFailedException(
                f"The pyrefly host at {root} returned no query result"
            )
        return result.raw

    # ------------------------------------------------------------------
    # Facade: identity, preconditions and the forwarded features
    # ------------------------------------------------------------------

    def _folder_settings(self) -> PyreflyFolderSettings:
        pyrefly = self._pyrefly_settings
        settings: PyreflyFolderSettings = {
            "pythonPath": self._python_path,
            "extraPaths": list(pyrefly.get("extraPaths", [])),
            "configPath": str(pyrefly.get("configPath", "")),
        }
        display_type_errors = pyrefly.get("displayTypeErrors")
        if display_type_errors is not None:
            settings["displayTypeErrors"] = display_type_errors
        return settings

    def _identity(self) -> PyreflyProjectIdentity:
        return PyreflyProjectIdentity.build(
            project_path=self._project_dir.as_posix(),
            version=self._read_pyrefly_version(),
            topology=self._topology,
            settings=self._folder_settings(),
        )

    async def _forward_root(self) -> Path:
        """The host project, after the preconditions that make a forward valid.

        Raises:
            ActionFailedException: no root, this project outside it, or the root
                does not declare the host action.
        """
        root = self._root_dir
        if root is None:
            raise code_action.ActionFailedException(
                "pyrefly workspace topology needs one workspace root: keep a"
                " single workspace directory, or mark it with"
                " finecode-workspace.toml."
            )
        if not self._project_dir.is_relative_to(root):
            raise code_action.ActionFailedException(
                f"Project {self._project_dir} is outside the workspace root"
                f" {root}. Move it under the root, or set topology ="
                ' "per-project" for every project.'
            )
        if not self._preconditions_checked:
            declares = await self._workspace_info_provider.project_declares_action(
                root, _HOST_ACTION_SOURCE
            )
            if not declares:
                raise code_action.ActionFailedException(
                    f"The workspace root {root} does not declare the pyrefly host"
                    f" action ({_HOST_ACTION_SOURCE}). Pyrefly must be in the"
                    " root project's venv."
                )
            self._preconditions_checked = True
        return root

    async def _announce(self) -> None:
        root = await self._forward_root()
        results = await self._workspace_action_runner.run_action_in_projects(
            AnnouncePyreflyProjectAction,
            AnnouncePyreflyProjectPayload(identity=self._identity()),
            meta=_FORWARD_META,
            project_paths=[root],
        )
        result = results.get(root)
        if result is None or not result.registered:
            error = (
                result.error if result is not None else "the host returned no result"
            )
            raise code_action.ActionFailedException(
                f"The pyrefly host at {root} refused project {self._project_dir}:"
                f" {error}"
            )

    def _inner(self) -> LspService:
        if self._lsp_service is None:
            raise code_action.ActionFailedException(
                "PyreflyLspService.init was not awaited before use"
            )
        return self._lsp_service

    def _position_query(
        self,
        kind: PyreflyPositionQueryKind,
        file_path: Path,
        content: str,
        position: dict[str, Any],
        include_declaration: bool = True,
    ) -> PyreflyPositionQuery:
        return PyreflyPositionQuery(
            kind=kind,
            file=ResourceUri(file_path.as_uri()),
            content=content,
            position_line=position["line"],
            position_character=position["character"],
            include_declaration=include_declaration,
        )

    async def get_hover(
        self,
        file_path: Path,
        content: str,
        position: dict[str, Any],
        timeout: float = 30.0,
    ) -> dict[str, Any] | None:
        if self._forwards:
            raw = await self._forward_query(
                self._position_query(
                    PyreflyQueryKind.HOVER, file_path, content, position
                )
            )
            return raw if isinstance(raw, dict) else None
        return await self._inner().get_hover(
            file_path, content, position, timeout=timeout
        )

    async def get_definition(
        self,
        file_path: Path,
        content: str,
        position: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | dict[str, Any] | None:
        if self._forwards:
            raw = await self._forward_query(
                self._position_query(
                    PyreflyQueryKind.DEFINITION, file_path, content, position
                )
            )
            return raw if isinstance(raw, (list, dict)) else None
        return await self._inner().get_definition(
            file_path, content, position, timeout=timeout
        )

    async def get_references(
        self,
        file_path: Path,
        content: str,
        position: dict[str, Any],
        include_declaration: bool = True,
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        if self._forwards:
            raw = await self._forward_query(
                self._position_query(
                    PyreflyQueryKind.REFERENCES,
                    file_path,
                    content,
                    position,
                    include_declaration=include_declaration,
                )
            )
            return raw if isinstance(raw, list) else None
        return await self._inner().get_references(
            file_path,
            content,
            position,
            include_declaration=include_declaration,
            timeout=timeout,
        )

    async def get_type_definition(
        self,
        file_path: Path,
        content: str,
        position: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | dict[str, Any] | None:
        if self._forwards:
            raw = await self._forward_query(
                self._position_query(
                    PyreflyQueryKind.TYPE_DEFINITION, file_path, content, position
                )
            )
            return raw if isinstance(raw, (list, dict)) else None
        return await self._inner().get_type_definition(
            file_path, content, position, timeout=timeout
        )

    async def get_implementation(
        self,
        file_path: Path,
        content: str,
        position: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | dict[str, Any] | None:
        if self._forwards:
            raw = await self._forward_query(
                self._position_query(
                    PyreflyQueryKind.IMPLEMENTATION, file_path, content, position
                )
            )
            return raw if isinstance(raw, (list, dict)) else None
        return await self._inner().get_implementation(
            file_path, content, position, timeout=timeout
        )

    async def get_document_highlight(
        self,
        file_path: Path,
        content: str,
        position: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        if self._forwards:
            raw = await self._forward_query(
                self._position_query(
                    PyreflyQueryKind.DOCUMENT_HIGHLIGHT, file_path, content, position
                )
            )
            return raw if isinstance(raw, list) else None
        return await self._inner().get_document_highlight(
            file_path, content, position, timeout=timeout
        )

    async def get_semantic_tokens(
        self,
        file_path: Path,
        content: str,
        range_dict: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any] | None:
        if self._forwards:
            start = range_dict.get("start") if range_dict is not None else None
            end = range_dict.get("end") if range_dict is not None else None
            raw = await self._forward_query(
                PyreflyRangeQuery(
                    kind=PyreflyQueryKind.SEMANTIC_TOKENS,
                    file=ResourceUri(file_path.as_uri()),
                    content=content,
                    start_line=start.get("line") if start is not None else None,
                    start_character=(
                        start.get("character") if start is not None else None
                    ),
                    end_line=end.get("line") if end is not None else None,
                    end_character=(
                        end.get("character") if end is not None else None
                    ),
                )
            )
            return raw if isinstance(raw, dict) else None
        return await self._inner().get_semantic_tokens(
            file_path, content, range_dict=range_dict, timeout=timeout
        )

    async def get_inlay_hints(
        self,
        file_path: Path,
        content: str,
        range_dict: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        if self._forwards:
            raw = await self._forward_query(
                PyreflyRangeQuery(
                    kind=PyreflyQueryKind.INLAY_HINTS,
                    file=ResourceUri(file_path.as_uri()),
                    content=content,
                    start_line=range_dict["start"]["line"],
                    start_character=range_dict["start"]["character"],
                    end_line=range_dict["end"]["line"],
                    end_character=range_dict["end"]["character"],
                )
            )
            return raw if isinstance(raw, list) else None
        return await self._inner().get_inlay_hints(
            file_path, content, range_dict, timeout=timeout
        )

    async def get_call_hierarchy_prepare(
        self,
        file_path: Path,
        content: str,
        position: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        if self._forwards:
            raw = await self._forward_query(
                self._position_query(
                    PyreflyQueryKind.CALL_HIERARCHY_PREPARE,
                    file_path,
                    content,
                    position,
                )
            )
            return raw if isinstance(raw, list) else None
        return await self._inner().get_call_hierarchy_prepare(
            file_path, content, position, timeout=timeout
        )

    async def get_call_hierarchy_incoming_calls(
        self,
        file_path: Path,
        content: str,
        item: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        if self._forwards:
            raw = await self._forward_query(
                PyreflyItemQuery(
                    kind=PyreflyQueryKind.CALL_HIERARCHY_INCOMING,
                    item=item,
                )
            )
            return raw if isinstance(raw, list) else None
        return await self._inner().get_call_hierarchy_incoming_calls(
            file_path, content, item, timeout=timeout
        )

    async def get_call_hierarchy_outgoing_calls(
        self,
        file_path: Path,
        content: str,
        item: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        if self._forwards:
            raw = await self._forward_query(
                PyreflyItemQuery(
                    kind=PyreflyQueryKind.CALL_HIERARCHY_OUTGOING,
                    item=item,
                )
            )
            return raw if isinstance(raw, list) else None
        return await self._inner().get_call_hierarchy_outgoing_calls(
            file_path, content, item, timeout=timeout
        )

    async def get_type_hierarchy_prepare(
        self,
        file_path: Path,
        content: str,
        position: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        if self._forwards:
            raw = await self._forward_query(
                self._position_query(
                    PyreflyQueryKind.TYPE_HIERARCHY_PREPARE,
                    file_path,
                    content,
                    position,
                )
            )
            return raw if isinstance(raw, list) else None
        return await self._inner().get_type_hierarchy_prepare(
            file_path, content, position, timeout=timeout
        )

    async def get_type_hierarchy_supertypes(
        self,
        file_path: Path,
        content: str,
        item: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        if self._forwards:
            raw = await self._forward_query(
                PyreflyItemQuery(
                    kind=PyreflyQueryKind.TYPE_HIERARCHY_SUPERTYPES,
                    item=item,
                )
            )
            return raw if isinstance(raw, list) else None
        return await self._inner().get_type_hierarchy_supertypes(
            file_path, content, item, timeout=timeout
        )

    async def get_type_hierarchy_subtypes(
        self,
        file_path: Path,
        content: str,
        item: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        if self._forwards:
            raw = await self._forward_query(
                PyreflyItemQuery(
                    kind=PyreflyQueryKind.TYPE_HIERARCHY_SUBTYPES,
                    item=item,
                )
            )
            return raw if isinstance(raw, list) else None
        return await self._inner().get_type_hierarchy_subtypes(
            file_path, content, item, timeout=timeout
        )

    # ------------------------------------------------------------------
    # Host: registration, coalesced pushes, per-scopeUri configuration
    # ------------------------------------------------------------------

    def register_project(self, identity: PyreflyProjectIdentity) -> None:
        """Register a forwarded project, refusing version/topology/path mismatches.

        The refusal names the projects the host has already heard from and the
        disagreeing one; the caller's project is not registered while
        already-registered projects keep working (D13).

        Raises:
            ActionFailedException: this service is not the host, the topology is
                mixed, the project is outside the root, or its pyrefly
                major.minor differs from the host's.
        """
        if not self._is_host:
            raise code_action.ActionFailedException(
                f"Received a pyrefly forward for {identity.project_path} in"
                f" {self._project_dir}, which is not the workspace host"
            )
        heard = ", ".join(sorted(self._project_registry)) or "none"
        if (
            identity.topology != PyreflyTopology.WORKSPACE
            or self._topology != PyreflyTopology.WORKSPACE
        ):
            raise code_action.ActionFailedException(
                f"Mixed pyrefly topologies: {identity.project_path} is"
                f" {identity.topology.value}, the host {self._project_dir} is"
                f" {self._topology.value}. Already heard from: {heard}. Use one"
                " topology for the workspace."
            )
        if self._root_dir is not None and not Path(identity.project_path).is_relative_to(
            self._root_dir
        ):
            raise code_action.ActionFailedException(
                f"Project {identity.project_path} is outside the workspace root"
                f" {self._root_dir}. Move it under the root, or set topology ="
                ' "per-project" for every project.'
            )
        identity_minor = self._major_minor(identity.version)
        host_minor = self._major_minor(self._host_version)
        if identity_minor is None:
            raise code_action.ActionFailedException(
                f"Project {identity.project_path} reports an unparsable pyrefly"
                f" version {identity.version!r}. One pyrefly major.minor is"
                " required per workspace."
            )
        if host_minor is None:
            raise code_action.ActionFailedException(
                f"The host reports an unparsable pyrefly version"
                f" {self._host_version!r}; cannot compare it with"
                f" {identity.project_path}'s {identity.version!r}."
            )
        if identity_minor != host_minor:
            raise code_action.ActionFailedException(
                f"Pyrefly version {identity.version!r} of"
                f" {identity.project_path} disagrees with the host's"
                f" {self._host_version!r}: one major.minor is required per"
                f" workspace. Already heard from: {heard}. Align the pins, or set"
                ' topology = "per-project".'
            )
        self._project_registry[identity.project_path] = identity

    async def ensure_forwarded(
        self,
        identity: PyreflyProjectIdentity,
        *,
        sweep_paths: list[Path],
    ) -> None:
        """Make the session hold *identity*: add/push/wait by the coalesce rule.

        Raises:
            ActionFailedException: the registration is refused, or the host did
                not answer a new/changed folder's configuration pull in time.
        """
        self.register_project(identity)
        self._last_activity = time.monotonic()
        folder_uri = Path(identity.project_path).as_uri()
        done = asyncio.Event()
        async with self._host_lock:
            if folder_uri not in self._session_folder_fingerprints:
                self._pending_added[folder_uri] = identity.project_path
            elif self._session_folder_fingerprints[folder_uri] != identity.fingerprint:
                self._pending_changed[folder_uri] = identity.fingerprint
            self._pending_sweeps.update(sweep_paths)
            self._pending_waiters.append(done)
            if self._flush_task is None or self._flush_task.done():
                self._flush_task = asyncio.create_task(self._flush_loop())
        await done.wait()
        errors = self._waiter_errors.pop(done, [])
        if errors:
            raise code_action.ActionFailedException("; ".join(errors))

    async def _flush_loop(self) -> None:
        """One flush per batch of forwards that arrived before it sent anything.

        A forward that arrives after the notifications went out appends to the
        next batch and waits for that one, as does a forward that arrives while
        the flush runs.
        """
        while True:
            async with self._host_lock:
                added = dict(self._pending_added)
                self._pending_added.clear()
                changed = dict(self._pending_changed)
                self._pending_changed.clear()
                sweeps = set(self._pending_sweeps)
                self._pending_sweeps.clear()
                waiters = list(self._pending_waiters)
                self._pending_waiters.clear()
                if not added and not changed and not sweeps:
                    # Nothing to push: an unchanged forward is ready as soon as
                    # it arrives. Not waking these waiters would hang it.
                    self._flush_task = None
                    for event in waiters:
                        self._waiter_errors[event] = []
                        event.set()
                    return
            errors = await self._send_flush(added, changed, sweeps)
            async with self._host_lock:
                for folder_uri, project_path in added.items():
                    identity = self._project_registry.get(project_path)
                    if identity is not None:
                        self._session_folder_fingerprints[folder_uri] = (
                            identity.fingerprint
                        )
                for folder_uri, fingerprint in changed.items():
                    self._session_folder_fingerprints[folder_uri] = fingerprint
            for event in waiters:
                self._waiter_errors[event] = list(errors)
                event.set()

    async def _send_flush(
        self,
        added: dict[str, str],
        changed: dict[str, str],
        sweeps: set[Path],
    ) -> list[str]:
        inner = self._inner()
        for folder_uri, name in added.items():
            await inner.add_workspace_folder(folder_uri, name)
        await inner.send_settings()

        errors: list[str] = []
        events: dict[str, RearmableLoopEvent] = {}
        for folder_uri in (*added, *changed):
            event = self._folder_pull_events.setdefault(
                folder_uri, RearmableLoopEvent()
            )
            event.clear()
            events[folder_uri] = event
        if events:

            async def wait_for_pull(folder_uri: str, event: RearmableLoopEvent) -> None:
                try:
                    await asyncio.wait_for(
                        event.wait_async(), _FOLDER_PULL_TIMEOUT_SEC
                    )
                except TimeoutError:
                    errors.append(
                        "The pyrefly host did not answer"
                        f" workspace/configuration for {folder_uri} within"
                        f" {_FOLDER_PULL_TIMEOUT_SEC:g}s"
                    )

            await asyncio.gather(
                *(wait_for_pull(uri, event) for uri, event in events.items())
            )
        if sweeps:
            await inner.sync_watched_files(sorted(sweeps), _RECHECK_BARRIER_SEC)
        return errors

    def _resolve_folder_configuration(self, scope_uri: str | None) -> dict[str, Any]:
        """Answer one ``workspace/configuration`` item by its folder.

        Records the scopeUri as answered (a flush waiting on it wakes) and
        returns that project's settings, or the host's own when no folder
        matches.
        """
        if scope_uri is None:
            return self._lsp_settings()
        event = self._folder_pull_events.get(scope_uri)
        if event is not None:
            event.set()
        project_path = resource_uri_to_path(ResourceUri(scope_uri)).as_posix()
        identity = self._project_registry.get(project_path)
        if identity is None:
            return self._lsp_settings()
        settings = identity.settings
        pyrefly: dict[str, Any] = {
            "extraPaths": list(settings["extraPaths"]),
            "configPath": settings["configPath"],
        }
        if settings.get("displayTypeErrors") is not None:
            pyrefly["displayTypeErrors"] = settings["displayTypeErrors"]
        return {"pythonPath": settings["pythonPath"], "pyrefly": pyrefly}

    def _lsp_settings(self) -> dict[str, Any]:
        return {
            "pythonPath": self._python_path,
            "pyrefly": dict(self._pyrefly_settings),
        }

    async def _idle_loop(self) -> None:
        """Stop the host server after D11's idle window, never counting a crash."""
        while True:
            await asyncio.sleep(_IDLE_SCAN_SEC)
            inner = self._lsp_service
            if inner is None:
                return
            if inner.has_open_documents:
                self._last_activity = time.monotonic()
                continue
            if time.monotonic() - self._last_activity < _IDLE_SHUTDOWN_SEC:
                continue
            self._logger.info("Stopping idle pyrefly host server")
            await inner.dispose()
            # The next demand re-establishes the session's folders; the
            # registered identities survive so their settings are still known.
            self._session_folder_fingerprints.clear()
            self._folder_pull_events.clear()
            self._last_activity = time.monotonic()

    @staticmethod
    def _read_pyrefly_version() -> str:
        try:
            return importlib.metadata.version("pyrefly")
        except importlib.metadata.PackageNotFoundError:
            return ""

    @staticmethod
    def _major_minor(version: str) -> tuple[int, int] | None:
        parts = version.split(".")
        if len(parts) < 2:
            return None
        try:
            return (int(parts[0]), int(parts[1]))
        except ValueError:
            return None
