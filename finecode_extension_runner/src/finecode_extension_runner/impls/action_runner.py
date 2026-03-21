import collections.abc
import typing
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iactionrunner

from finecode_extension_runner import domain


class ActionRunner(iactionrunner.IActionRunner):
    def __init__(self, run_action_func: typing.Callable[[domain.ActionDeclaration, code_action.RunActionPayload, code_action.RunActionMeta], collections.abc.Coroutine[None, None, code_action.RunActionResult]],
                 actions_names_getter: typing.Callable[[], list[str]],
    action_by_name_getter: typing.Callable[[str], domain.ActionDeclaration]):
        self._run_action_func = run_action_func
        self._actions_names_getter = actions_names_getter
        self._action_by_name_getter = action_by_name_getter

    @typing.override
    async def run_action(
        self, action: iactionrunner.ActionDeclaration[iactionrunner.ActionT], payload: code_action.RunActionPayload, meta: code_action.RunActionMeta
    ) -> code_action.RunActionResult:
        try:
            return await self._run_action_func(action, payload, meta)
        except Exception as exception:
            raise iactionrunner.ActionRunFailed(str(exception)) from exception

    @typing.override
    def get_actions_names(self) -> list[str]:
        return self._actions_names_getter()
    
    @typing.override
    def get_actions_by_source(self, action_type: type[iactionrunner.ActionT]) -> list[iactionrunner.ActionDeclaration[iactionrunner.ActionT]]:
        return [
            action
            for name in self._actions_names_getter()
            if (action := self._action_by_name_getter(name)).source.rsplit(".", 1)[-1] == action_type.__name__
        ]

    @typing.override
    def get_action_by_name(self, name: str, action_type: type[iactionrunner.ActionT]) -> iactionrunner.ActionDeclaration[iactionrunner.ActionT]:
        try:
            return self._action_by_name_getter(name)
        except KeyError as exception:
            raise iactionrunner.ActionNotFound(f"Action '{name}' not found") from exception

    @typing.override
    def get_actions_for_language(self, action_type: type[iactionrunner.ActionT], language: str) -> list[iactionrunner.ActionDeclaration[iactionrunner.ActionT]]:
        return [
            action for action in self.get_actions_by_source(action_type=action_type) if action.name.endswith('_' + language)
        ]
