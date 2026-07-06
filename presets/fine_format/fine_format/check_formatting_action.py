# docs: docs/reference/actions.md
import dataclasses
import sys

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.resource_uri import ResourceUri
from fine_format.format_action import FormatTarget


@dataclasses.dataclass
class CheckFormattingRunPayload(code_action.RunActionPayload):
    target: FormatTarget = FormatTarget.PROJECT
    """Scope of the check: 'project' (default) checks the whole project, 'files' checks only file_paths."""
    file_paths: list[ResourceUri] = dataclasses.field(default_factory=list)
    """Files to check (``file://`` URIs). Only used when target is 'files'."""


class CheckFormattingRunContext(
    code_action.RunActionContext[CheckFormattingRunPayload]
): ...


@dataclasses.dataclass
class CheckFormattingRunResult(code_action.RunActionResult):
    """Files that would be changed by ``format``, without writing them.

    Same aggregation contract as ``FormatFilesRunResult.update`` — a file
    already reported as needing formatting stays reported even if a later
    partial doesn't repeat it.
    """

    files_needing_format: list[ResourceUri] = dataclasses.field(default_factory=list)

    @override
    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, CheckFormattingRunResult):
            return

        existing = set(self.files_needing_format)
        for file_uri in other.files_needing_format:
            if file_uri not in existing:
                self.files_needing_format.append(file_uri)
                existing.add(file_uri)

    @override
    def to_text(self) -> str | textstyler.StyledText:
        text = textstyler.StyledText()
        if self.files_needing_format:
            for file_uri in self.files_needing_format:
                text.append_styled(str(file_uri), bold=True)
                text.append(": needs formatting\n")
        else:
            text.append("All files formatted correctly.\n")
        return text

    @property
    @override
    def return_code(self) -> code_action.RunReturnCode:
        if self.files_needing_format:
            return code_action.RunReturnCode.ERROR
        return code_action.RunReturnCode.SUCCESS


class CheckFormattingAction(
    code_action.Action[
        CheckFormattingRunPayload, CheckFormattingRunContext, CheckFormattingRunResult
    ]
):
    """Check whether source code is formatted, without writing changes."""

    DESCRIPTION = "Check whether source code is formatted, without writing changes."
    PAYLOAD_TYPE = CheckFormattingRunPayload
    RUN_CONTEXT_TYPE = CheckFormattingRunContext
    RESULT_TYPE = CheckFormattingRunResult
