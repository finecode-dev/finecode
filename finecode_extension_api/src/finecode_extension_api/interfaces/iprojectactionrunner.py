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


class IProjectActionRunner(service.Service, typing.Protocol):
    """Run an action at project scope — across all env-runners of one project.

    Callers identify actions by their Python type. The implementation derives
    the import-path source string, calls the WM back-channel
    ``finecode/runActionInProject``, and deserializes the response using the
    action class's RESULT_TYPE. This is transparent to the handler author.

    The runner automatically detects when all handlers for an action are
    registered in the current env and skips the WM round-trip for performance.
    Callers do not need to be aware of this optimisation.
    """

    def get_actions_for_parent(
        self, parent_action_type: type[ActionT]
    ) -> dict[str, type]:
        """Return language-keyed subaction types registered for *parent_action_type*.

        Returns a ``{language: ActionClass}`` mapping for all locally-registered
        subactions whose ``PARENT_ACTION`` attribute matches *parent_action_type*.
        The returned types can be passed directly to ``run_action`` /
        ``run_action_iter`` as ``action_type``.

        This is a local-only lookup — subactions registered in other envs are
        not visible here, but language dispatch is always same-env by convention.
        """
        ...

    async def run_action(
        self,
        action_type: type[code_action.Action[PayloadT, typing.Any, ResultT]],
        payload: PayloadT,
        meta: code_action.RunActionMeta,
        caller_kwargs: code_action.CallerRunContextKwargs | None = None,
    ) -> ResultT: ...

    def run_action_iter(
        self,
        action_type: type[code_action.Action[PayloadT, typing.Any, ResultT]],
        payload: PayloadT,
        meta: code_action.RunActionMeta,
        caller_kwargs: code_action.CallerRunContextKwargs | None = None,
    ) -> collections.abc.AsyncIterator[ResultT]: ...


class BaseRunActionException(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


class ActionNotFound(BaseRunActionException): ...


class InvalidActionRunPayload(BaseRunActionException): ...


class ActionRunFailed(BaseRunActionException): ...
