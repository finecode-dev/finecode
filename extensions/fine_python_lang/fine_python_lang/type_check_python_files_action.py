from finecode_extension_api import code_action
from fine_type_check.diagnostic_types import (
    DiagnosticFilesRunPayload,
    DiagnosticFilesRunContext,
    DiagnosticFilesRunResult,
)
from fine_type_check.type_check_files_action import TypeCheckFilesAction


class TypeCheckPythonFilesAction(
    code_action.Action[
        DiagnosticFilesRunPayload,
        DiagnosticFilesRunContext,
        DiagnosticFilesRunResult,
    ]
):
    """Type-check Python source files and report type errors.

    Handler recommendation — env for import resolution:
    A type checker needs one environment to resolve the imports of the code it
    checks. Test files often pull in dev-only dependencies (e.g. pytest) that are
    absent from the "runtime" env, so resolving everything against "runtime" makes
    those imports unresolvable. Handlers should therefore resolve imports against
    the "dev" env (a superset of "runtime": project deps + dev-only deps), falling
    back to "runtime" when no "dev" env exists. Falling back only loses symbols,
    never adds false ones.

    The trade-off is that dev-only deps then resolve from source files too, so the
    type checker no longer flags a dev dependency imported from source. That
    boundary is intentionally delegated to a dependency-hygiene tool (e.g. deptry),
    which must be wired into the same gate so the boundary is still enforced.

    Note: "dev"/"runtime" are naming conventions — env labels are arbitrary — so
    this env selection could be made configurable in the future, either per handler
    or at the action level (one resolution-env policy shared by all of the action's
    handlers).
    """

    DESCRIPTION = "Type-check Python source files and report type errors."
    PAYLOAD_TYPE = DiagnosticFilesRunPayload
    RUN_CONTEXT_TYPE = DiagnosticFilesRunContext
    RESULT_TYPE = DiagnosticFilesRunResult
    LANGUAGE = "python"
    PARENT_ACTION = TypeCheckFilesAction
    HANDLER_EXECUTION = code_action.HandlerExecution.CONCURRENT
