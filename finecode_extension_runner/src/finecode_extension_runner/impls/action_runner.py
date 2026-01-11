import collections.abc
import typing
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iactionrunner

from finecode_extension_runner import domain


class ActionRunner(iactionrunner.IActionRunner):
    def __init__(self, run_action_func: typing.Callable[[domain.Action, code_action.RunActionPayload, code_action.RunActionMeta], collections.abc.Coroutine[None, None, code_action.RunActionResult]],
                 actions_names_getter: typing.Callable[[], list[str]],
    action_by_name_getter: typing.Callable[[str], domain.Action]):
        self._run_action_func = run_action_func
        self._actions_names_getter = actions_names_getter
        self._action_by_name_getter = action_by_name_getter

    async def run_action(
        self, action: type[code_action.Action[code_action.RunPayloadType, code_action.RunContextType, code_action.RunResultType]], payload: code_action.RunActionPayload, meta: code_action.RunActionMeta
    ) -> code_action.RunActionResult:
        try:
            return await self._run_action_func(action, payload, meta)
        except Exception as exception:
            raise iactionrunner.ActionRunFailed(str(exception))

    def get_actions_names(self) -> list[str]:
        return self._actions_names_getter()

    def get_action_by_name(self, name: str) -> type[code_action.Action[code_action.RunPayloadType, code_action.RunContextType, code_action.RunResultType]]:
        try:
            return self._action_by_name_getter(name)
        except KeyError:
            raise iactionrunner.ActionNotFound(f"Action '{name}' not found")
