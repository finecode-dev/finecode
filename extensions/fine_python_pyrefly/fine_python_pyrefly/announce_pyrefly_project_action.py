from finecode_extension_api import code_action

from fine_python_pyrefly.host_types import (
    AnnouncePyreflyProjectPayload,
    AnnouncePyreflyProjectResult,
)


class AnnouncePyreflyProjectAction(
    code_action.Action[
        AnnouncePyreflyProjectPayload,
        code_action.RunActionContext[AnnouncePyreflyProjectPayload],
        AnnouncePyreflyProjectResult,
    ]
):
    """Register a project with the shared pyrefly server.

    Pyrefly host transport, not a user tool: it exists so a non-root project can
    hand the host its identity (interpreter, extra paths, rendered config,
    version) before its first forward (ADR-0108 rule 2).
    """

    DESCRIPTION = (
        "Pyrefly host transport: register a project's identity with the shared"
        " pyrefly server."
    )
    SCOPE = code_action.ActionScope.PROJECT
    PAYLOAD_TYPE = AnnouncePyreflyProjectPayload
    RUN_CONTEXT_TYPE = code_action.RunActionContext
    RESULT_TYPE = AnnouncePyreflyProjectResult
