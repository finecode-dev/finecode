# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action
from fine_envs.list_obtainable_toolchains_action import (
    ListObtainableToolchainsAction,
    ListObtainableToolchainsRunPayload,
    ListObtainableToolchainsRunResult,
)


@dataclasses.dataclass
class ListObtainablePythonInterpretersRunPayload(ListObtainableToolchainsRunPayload):
    """Same payload as the parent."""


class ListObtainablePythonInterpretersRunContext(
    code_action.RunActionContext[ListObtainablePythonInterpretersRunPayload]
): ...


class ListObtainablePythonInterpretersAction(
    code_action.Action[
        ListObtainablePythonInterpretersRunPayload,
        ListObtainablePythonInterpretersRunContext,
        ListObtainableToolchainsRunResult,
    ]
):
    """List the Python interpreters the environment provisioner can obtain.

    Returns canonical ``<implementation>@<minor version>`` identities across every
    implementation the provisioner offers, stable releases only by default.
    """

    DESCRIPTION = "List the Python interpreters the environment provisioner can obtain."
    PAYLOAD_TYPE = ListObtainablePythonInterpretersRunPayload
    RUN_CONTEXT_TYPE = ListObtainablePythonInterpretersRunContext
    RESULT_TYPE = ListObtainableToolchainsRunResult
    LANGUAGE = "python"
    PARENT_ACTION = ListObtainableToolchainsAction
