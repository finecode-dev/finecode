from finecode_extension_api.code_action import Action, ActionScope

from .consts import SOME_CONST


class NonLiteralAction(Action):
    LANGUAGE = SOME_CONST
    SCOPE = ActionScope.WORKSPACE
