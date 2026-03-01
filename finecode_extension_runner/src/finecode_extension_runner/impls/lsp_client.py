from __future__ import annotations

import collections.abc
import os
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from finecode_extension_api.interfaces import ijsonrpcclient, ilspclient


class LspSessionImpl(ilspclient.ILspSession):
    """ILspSession implementation. Wraps an IJsonRpcSession with LSP lifecycle."""

    def __init__(
        self,
        json_rpc_session: ijsonrpcclient.IJsonRpcSession,
        root_uri: str,
        workspace_folders: list[dict[str, str]] | None,
        initialization_options: dict[str, Any] | None,
        client_capabilities: dict[str, Any] | None,
    ) -> None:
        self._session = json_rpc_session
        self._root_uri = root_uri
        self._workspace_folders = workspace_folders
        self._initialization_options = initialization_options
        self._client_capabilities = (
            client_capabilities
            if client_capabilities is not None
            else _default_client_capabilities()
        )
        self._server_capabilities: dict[str, Any] = {}
        self._server_info: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> Self:
        await self._session.__aenter__()

        # LSP initialize handshake
        init_params: dict[str, Any] = {
            "processId": os.getpid(),
            "rootUri": self._root_uri,
            "capabilities": self._client_capabilities,
        }
        if self._workspace_folders is not None:
            init_params["workspaceFolders"] = self._workspace_folders
        if self._initialization_options is not None:
            init_params["initializationOptions"] = self._initialization_options

        init_result = await self._session.send_request(
            "initialize", init_params, timeout=30.0
        )

        if isinstance(init_result, dict):
            self._server_capabilities = init_result.get("capabilities", {})
            self._server_info = init_result.get("serverInfo")

        await self._session.send_notification("initialized", {})

        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        try:
            await self._session.send_request("shutdown", timeout=10.0)
        except Exception:
            pass  # Best effort

        try:
            await self._session.send_notification("exit")
        except Exception:
            pass  # Best effort

        await self._session.__aexit__(exc_type, exc_val, exc_tb)

    # ------------------------------------------------------------------
    # Async API (delegated)
    # ------------------------------------------------------------------

    async def send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        return await self._session.send_request(method, params, timeout)

    async def send_notification(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        await self._session.send_notification(method, params)

    # ------------------------------------------------------------------
    # Sync API (delegated)
    # ------------------------------------------------------------------

    def send_request_sync(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        return self._session.send_request_sync(method, params, timeout)

    def send_notification_sync(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        self._session.send_notification_sync(method, params)

    # ------------------------------------------------------------------
    # Server-initiated messages (delegated)
    # ------------------------------------------------------------------

    def on_notification(
        self,
        method: str,
        handler: collections.abc.Callable[
            [dict[str, Any] | None], collections.abc.Awaitable[None]
        ],
    ) -> None:
        self._session.on_notification(method, handler)

    def on_request(
        self,
        method: str,
        handler: collections.abc.Callable[
            [dict[str, Any] | None], collections.abc.Awaitable[Any]
        ],
    ) -> None:
        self._session.on_request(method, handler)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def server_capabilities(self) -> dict[str, Any]:
        return self._server_capabilities

    @property
    def server_info(self) -> dict[str, Any] | None:
        return self._server_info


class LspClientImpl(ilspclient.ILspClient):
    """ILspClient implementation. Factory for LspSessionImpl."""

    def __init__(self, json_rpc_client: ijsonrpcclient.IJsonRpcClient) -> None:
        self._json_rpc_client = json_rpc_client

    def session(
        self,
        cmd: str,
        root_uri: str,
        workspace_folders: list[dict[str, str]] | None = None,
        initialization_options: dict[str, Any] | None = None,
        client_capabilities: dict[str, Any] | None = None,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        readable_id: str = "",
    ) -> LspSessionImpl:
        json_rpc_session = self._json_rpc_client.session(
            cmd=cmd, cwd=cwd, env=env, readable_id=readable_id
        )
        return LspSessionImpl(
            json_rpc_session=json_rpc_session,
            root_uri=root_uri,
            workspace_folders=workspace_folders,
            initialization_options=initialization_options,
            client_capabilities=client_capabilities,
        )


def _default_client_capabilities() -> dict[str, Any]:
    return {
        "textDocument": {
            "synchronization": {
                "dynamicRegistration": False,
                "didSave": True,
            },
            "completion": {"dynamicRegistration": False},
            "hover": {"dynamicRegistration": False},
            "publishDiagnostics": {"relatedInformation": True},
        },
        "workspace": {
            "workspaceFolders": True,
            "configuration": True,
        },
    }
