import typing

from finecode_extension_api import code_action, service



class IActionRunner(service.Service, typing.Protocol):
    async def run_action(
        self, action: type[code_action.Action[code_action.RunPayloadType, code_action.RunContextType, code_action.RunResultType]], payload: code_action.RunPayloadType, meta: code_action.RunActionMeta
    ) -> code_action.RunResultType: ...

    def get_actions_names(self) -> list[str]:
        ...

    def get_action_by_name(self, name: str) -> type[code_action.Action[code_action.RunPayloadType, code_action.RunContextType, code_action.RunResultType]]:
        ...


class BaseRunActionException(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


class ActionNotFound(BaseRunActionException): ...


class InvalidActionRunPayload(BaseRunActionException): ...


class ActionRunFailed(BaseRunActionException): ...
