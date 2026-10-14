from finecode_extension_api import code_action

from fine_python_pyrefly.host_types import (
    CheckFilesOnPyreflyHostPayload,
    CheckFilesOnPyreflyHostResult,
)


class CheckFilesOnPyreflyHostAction(
    code_action.Action[
        CheckFilesOnPyreflyHostPayload,
        code_action.RunActionContext[CheckFilesOnPyreflyHostPayload],
        CheckFilesOnPyreflyHostResult,
    ]
):
    """Check one chunk of a project's files on the shared pyrefly server.

    Pyrefly host transport, not a user tool. The caller chunks a run and sends
    at most 50 files per forward; every file comes back as a checked outcome or
    a failed one, so the caller can keep a per-file error channel (ADR-0108
    rule 3).
    """

    DESCRIPTION = (
        "Pyrefly host transport: check a chunk of files on the shared pyrefly"
        " server."
    )
    SCOPE = code_action.ActionScope.PROJECT
    PAYLOAD_TYPE = CheckFilesOnPyreflyHostPayload
    RUN_CONTEXT_TYPE = code_action.RunActionContext
    RESULT_TYPE = CheckFilesOnPyreflyHostResult
