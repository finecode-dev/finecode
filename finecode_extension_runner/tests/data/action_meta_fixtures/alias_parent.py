from finecode_extension_api.code_action import Action

from . import reexport_mid as mid


class AliasParentAction(Action):
    PARENT_ACTION = mid.ParentAction
    LANGUAGE = "python"
