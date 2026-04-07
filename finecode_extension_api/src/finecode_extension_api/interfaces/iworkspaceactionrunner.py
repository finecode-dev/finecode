from __future__ import annotations

import pathlib
import typing

from finecode_extension_api import code_action, service
from finecode_extension_api.interfaces.iactionrunner import ActionDeclaration

PayloadT = typing.TypeVar("PayloadT", bound=code_action.RunActionPayload)
ResultT = typing.TypeVar("ResultT", bound=code_action.RunActionResult)
ActionT = typing.TypeVar(
    "ActionT",
    bound=code_action.Action[typing.Any, typing.Any, typing.Any],
    covariant=True,
)


class IWorkspaceActionRunner(service.Service, typing.Protocol):
    """Fan-out an action across all workspace projects that declare it.

    project_paths=None means all projects in the workspace that declare
    the action.
    """

    async def run_action_in_projects(
        self,
        action: ActionDeclaration[code_action.Action[PayloadT, typing.Any, ResultT]],
        payload: PayloadT,
        meta: code_action.RunActionMeta,
        project_paths: list[pathlib.Path] | None = None,
        concurrently: bool = True,
    ) -> dict[pathlib.Path, ResultT]: ...
