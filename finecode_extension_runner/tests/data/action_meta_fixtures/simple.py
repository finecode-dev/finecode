from finecode_extension_api.code_action import Action, ActionScope


class SimpleAction(Action):
    LANGUAGE = "python"
    SCOPE = ActionScope.WORKSPACE
