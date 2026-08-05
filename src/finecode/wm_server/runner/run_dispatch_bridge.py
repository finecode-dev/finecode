"""Slot through which whoever owns action execution serves the runner layer.

``runActionInProject``, ``runActionInWorkspace``, and ``getActionsForParent`` arrive on
the **runner**'s JSON-RPC client — an ER asking the WM to run an action (its own or
another project's) or to resolve its action tree. Answering them means executing an
action end-to-end (env selection, ER dispatch, orchestration depth, ...), which is
``services/run_service``'s job, and services sits *above* the runner in the WM's layer
stack. See ADR-0072 for why this is a slot the owner fills on import rather than an
upward import.
"""

from __future__ import annotations

import typing

if typing.TYPE_CHECKING:
    from finecode.wm_server import context
    from finecode.wm_server.runner import _internal_client_types
    from finecode.wm_server.runner.runner_client import ExtensionRunnerInfo

__all__ = ["RunDispatchHandlers", "handlers", "install", "reset"]


class RunDispatchHandlers(typing.Protocol):
    """What the runner needs from whoever owns action execution."""

    async def run_action_in_project(
        self,
        runner: ExtensionRunnerInfo,
        params: _internal_client_types.RunActionInProjectParams,
        ws_context: context.WorkspaceContext,
    ) -> _internal_client_types.RunActionInProjectResult:
        """Execute an action an ER asked the WM to run in its own project.

        Raises:
            ActionRunFailed: the action could not be dispatched, or a handler failed.
        """

    async def run_action_in_workspace(
        self,
        runner: ExtensionRunnerInfo,
        params: _internal_client_types.RunActionInWorkspaceParams,
        ws_context: context.WorkspaceContext,
    ) -> _internal_client_types.RunActionInWorkspaceResult:
        """Fan an ER-originated action out across the projects that declare it.

        Raises:
            ActionNotFoundError: no action in the calling project has this source.
            ActionRunFailed: the action could not be dispatched, or a handler failed.
            InternalError: the calling project has no valid config.
        """

    async def get_actions_for_parent(
        self,
        runner: ExtensionRunnerInfo,
        params: _internal_client_types.GetActionsForParentParams,
        ws_context: context.WorkspaceContext,
    ) -> _internal_client_types.GetActionsForParentResult:
        """List every action in this project that specializes the given parent action.

        Raises:
            ConfigurationError: the calling project has no valid config.
        """


_installed: RunDispatchHandlers | None = None


def install(implementation: RunDispatchHandlers) -> None:
    """Nominate *implementation* as the answer to run-dispatch requests from an ER."""
    global _installed
    _installed = implementation


def reset() -> None:
    """Forget the installed implementation. Tests only."""
    global _installed
    _installed = None


def handlers() -> RunDispatchHandlers | None:
    """The installed implementation, or ``None`` if the service was never imported.

    ``None`` is a real state rather than a defect: a WM built without run_service
    answers these RPCs with an error, which is what an ER asking a WM that cannot
    dispatch runs should hear. Unlike ``wm_bridge``, there is no useful null
    implementation — the caller is waiting on a result only the service can produce.
    """
    return _installed
