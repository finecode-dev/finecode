from __future__ import annotations

import collections.abc
import dataclasses
import typing

from finecode_extension_api import code_action, service

PayloadT = typing.TypeVar("PayloadT", bound=code_action.RunActionPayload)
ResultT = typing.TypeVar("ResultT", bound=code_action.RunActionResult)
ActionT = typing.TypeVar(
    "ActionT",
    bound=code_action.Action[typing.Any, typing.Any, typing.Any],
    covariant=True,
)


@dataclasses.dataclass(frozen=True)
class ActionRef:
    """A reference to an action identified by its canonical source and optionally its type.

    ``source`` is always present and has the form ``"module.ClassName"``.
    ``result_type`` is the concrete type used to deserialize the raw result dict;
    it is always set so that ``run_action`` / ``run_action_iter`` always return a
    properly structured result — never a raw dict.
    ``action_type`` is set when the action class is importable in the current env;
    ``None`` when resolved only via WM metadata fallback.

    **Two usage patterns**

    1. *Known action* — the caller knows exactly which action to run and it is
       locally importable.  Use ``ActionRef.from_type``::

           await runner.run_action(ActionRef.from_type(LintFilesAction), payload, meta)

       ``result_type`` is set to ``LintFilesAction.RESULT_TYPE`` automatically.

    2. *Dynamically discovered subactions* — the caller does not know in advance
       which concrete subactions are registered (e.g. language-specific subactions
       discovered via ``get_actions_for_parent``).  The ``ActionRef`` returned may
       have ``action_type=None`` when the subaction class lives in another env and
       cannot be imported locally.

       In that case ``result_type`` is set to the *parent* action's ``RESULT_TYPE``
       by ``get_actions_for_parent``.  This is safe because every subaction's result
       type is contractually compatible with (a subtype of) the parent's result type.
       The runner structures the raw result into that type transparently — no manual
       cast is needed at call sites.
    """

    source: str
    result_type: type
    action_type: type | None = None

    @classmethod
    def from_type(cls, action_type: type) -> ActionRef:
        """Construct an ``ActionRef`` from a locally available action class."""
        return cls(
            source=f"{action_type.__module__}.{action_type.__qualname__}",
            result_type=action_type.RESULT_TYPE,
            action_type=action_type,
        )


class IProjectActionRunner(service.Service, typing.Protocol):
    """Run an action at project scope — across all env-runners of one project.

    Callers identify actions by an ``ActionRef``. The implementation uses the
    ref's source string to call the WM back-channel ``finecode/runActionInProject``
    and the ref's type (when available) to deserialize the response. This is
    transparent to the handler author.

    The runner automatically detects when all handlers for an action are
    registered in the current env and skips the WM round-trip for performance.
    Callers do not need to be aware of this optimisation.
    """

    async def get_actions_for_parent(
        self, parent_action_type: type[ActionT]
    ) -> dict[str, ActionRef]:
        """Return language-keyed ``ActionRef`` objects registered for *parent_action_type*.

        Returns a ``{language: ActionRef}`` mapping. ``ActionRef.action_type`` is
        set when the class is importable locally; ``ActionRef.source`` is always
        present. Both forms can be passed directly to ``run_action`` /
        ``run_action_iter``.
        """
        ...

    async def run_action(
        self,
        action_type: ActionRef,
        payload: PayloadT,
        meta: code_action.RunActionMeta,
        caller_kwargs: code_action.CallerRunContextKwargs | None = None,
    ) -> ResultT: ...

    def run_action_iter(
        self,
        action_type: ActionRef,
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


class ActionRunCancelled(BaseRunActionException): ...
