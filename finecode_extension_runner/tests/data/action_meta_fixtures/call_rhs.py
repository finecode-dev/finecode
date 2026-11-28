from finecode_extension_api.code_action import Action


def compute():
    return "python"


class CallRhsAction(Action):
    LANGUAGE = compute()
