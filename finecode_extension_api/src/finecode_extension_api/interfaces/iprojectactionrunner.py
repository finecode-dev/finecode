from __future__ import annotations

import collections.abc
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


class IProjectActionRunner(service.Service, typing.Protocol):
    """Run an action at project scope — across all env-runners of one project.

    API mirrors IActionRunner: actions are identified by ActionDeclaration
    (which carries the import-path source string). No caller_kwargs — caller
    context does not cross process boundaries.

    The implementation serializes payload to a dict, calls the WM back-channel
    with the action's source path, and deserializes the response using the
    action class's RESULT_TYPE. This is transparent to the handler author.
    """

    async def run_action(
        self,
        action: ActionDeclaration[code_action.Action[PayloadT, typing.Any, ResultT]],
        payload: PayloadT,
        meta: code_action.RunActionMeta,
    ) -> ResultT: ...

    def run_action_iter(
        self,
        action: ActionDeclaration[code_action.Action[PayloadT, typing.Any, ResultT]],
        payload: PayloadT,
        meta: code_action.RunActionMeta,
    ) -> collections.abc.AsyncIterator[ResultT]: ...
