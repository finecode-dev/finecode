from finecode_extension_api.code_action import Action

from .base import BaseAction


class ChildAction(Action):
    PARENT_ACTION = BaseAction
    LANGUAGE = "python"
