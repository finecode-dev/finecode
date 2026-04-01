from __future__ import annotations

import collections.abc
import typing

from finecode_extension_api import code_action, service

PayloadT = typing.TypeVar("PayloadT", bound=code_action.RunActionPayload)
ResultT = typing.TypeVar("ResultT", bound=code_action.RunActionResult)
ActionT = typing.TypeVar(
    "ActionT",
    bound=code_action.Action[typing.Any, typing.Any, typing.Any],
    covariant=True,
)


class ActionDeclaration(typing.Generic[ActionT]): ...


class IActionRunner(service.Service, typing.Protocol):
    def get_action_by_source(
        self, action_type: type[ActionT]
    ) -> ActionDeclaration[ActionT]: ...

    def get_actions_for_parent(
        self, parent_action_type: type[ActionT]
    ) -> dict[str, ActionDeclaration[ActionT]]: ...

    def get_action_by_name(
        self, name: str, action_type: type[ActionT]
    ) -> ActionDeclaration[ActionT]:
        """Prefer `get_action_by_source`"""
        ...

    async def run_action(
        self,
        action: ActionDeclaration[code_action.Action[PayloadT, typing.Any, ResultT]],
        payload: PayloadT,
        meta: code_action.RunActionMeta,
        caller_kwargs: code_action.CallerRunContextKwargs | None = None,
    ) -> ResultT: ...

    def run_action_iter(
        self,
        action: ActionDeclaration[code_action.Action[PayloadT, typing.Any, ResultT]],
        payload: PayloadT,
        meta: code_action.RunActionMeta,
        caller_kwargs: code_action.CallerRunContextKwargs | None = None,
    ) -> collections.abc.AsyncIterator[ResultT]: ...

    def get_actions_names(self) -> list[str]: ...


class BaseRunActionException(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


class ActionNotFound(BaseRunActionException): ...


class InvalidActionRunPayload(BaseRunActionException): ...


class ActionRunFailed(BaseRunActionException): ...
