from finecode_extension_api import code_action

from fine_python_pyrefly.host_types import (
    QueryPyreflyHostPayload,
    QueryPyreflyHostResult,
)


class QueryPyreflyHostAction(
    code_action.Action[
        QueryPyreflyHostPayload,
        code_action.RunActionContext[QueryPyreflyHostPayload],
        QueryPyreflyHostResult,
    ]
):
    """Answer one IDE query on the shared pyrefly server.

    Pyrefly host transport, not a user tool: hover, definition, references and
    the other document features forwarded by a non-root project (ADR-0108
    rule 3).
    """

    DESCRIPTION = (
        "Pyrefly host transport: answer an IDE query on the shared pyrefly"
        " server."
    )
    SCOPE = code_action.ActionScope.PROJECT
    PAYLOAD_TYPE = QueryPyreflyHostPayload
    RUN_CONTEXT_TYPE = code_action.RunActionContext
    RESULT_TYPE = QueryPyreflyHostResult
