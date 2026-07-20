# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action
from fine_envs.sync_toolchains_action import (
    SyncToolchainsAction,
    SyncToolchainsRunPayload,
    SyncToolchainsRunResult,
)


@dataclasses.dataclass
class SyncPythonInterpretersRunPayload(SyncToolchainsRunPayload):
    """Same payload as the parent.

    The Python source takes no extra caller-facing parameters: which envs derive an
    axis, and how the derivation is tuned, is derivation intent and lives in handler
    config (ADR-0053).
    """


class SyncPythonInterpretersRunContext(
    code_action.RunActionContext[SyncPythonInterpretersRunPayload]
): ...


class SyncPythonInterpretersAction(
    code_action.Action[
        SyncPythonInterpretersRunPayload,
        SyncPythonInterpretersRunContext,
        SyncToolchainsRunResult,
    ]
):
    """Derive an env's Python interpreter axis.

    The Python materialization of ``sync_toolchains``: a toolchain here is an
    interpreter, identified by implementation and version together (``cpython@3.12``).

    Where the supported versions are declared, and how they expand into identities, is
    the handler's business.
    """

    DESCRIPTION = (
        "Derive an env's Python interpreter axis from the project's declared support."
    )
    PAYLOAD_TYPE = SyncPythonInterpretersRunPayload
    RUN_CONTEXT_TYPE = SyncPythonInterpretersRunContext
    RESULT_TYPE = SyncToolchainsRunResult
    LANGUAGE = "python"
    PARENT_ACTION = SyncToolchainsAction
