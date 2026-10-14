from finecode_extension_api.code_action import Action

from .consts import DEFAULT_SCOPE


class AliasScopeAction(Action):
    SCOPE = DEFAULT_SCOPE
    LANGUAGE = "python"
