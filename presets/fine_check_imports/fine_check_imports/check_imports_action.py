# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action
from fine_inspect_code.diagnostic_types import DiagnosticFilesRunResult
from finecode_extension_api.resource_uri import ResourceUri


@dataclasses.dataclass
class CheckImportsRunPayload(code_action.RunActionPayload):
    src_artifact_def_path: ResourceUri | None = None
    """Path to the artifact definition file (e.g. pyproject.toml). None = the current project's own definition file."""


@dataclasses.dataclass
class CheckImportsRunResult(DiagnosticFilesRunResult): ...


class CheckImportsRunContext(
    code_action.RunActionWithPartialResultsContext[CheckImportsRunPayload]
): ...


class CheckImportsAction(
    code_action.Action[CheckImportsRunPayload, CheckImportsRunContext, CheckImportsRunResult]
):
    """Check a project's import graph against configured architectural contracts.

    Whole-project scope: analyzes the full import graph (e.g. via import-linter),
    not individual files — a violation is a relationship between modules, not a
    property of one file. A project that has no import-graph tooling configured
    is a no-op: the result has empty ``messages``, not an error.
    """

    DESCRIPTION = "Check import-graph architectural contracts and report diagnostics."
    PAYLOAD_TYPE = CheckImportsRunPayload
    RUN_CONTEXT_TYPE = CheckImportsRunContext
    RESULT_TYPE = CheckImportsRunResult
