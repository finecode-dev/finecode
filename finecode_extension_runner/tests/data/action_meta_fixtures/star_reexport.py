from finecode_extension_api.code_action import Action

from .consts import *  # noqa: F403 - fixture for fail-closed guard


class StarAction(Action):
    LANGUAGE = "python"
