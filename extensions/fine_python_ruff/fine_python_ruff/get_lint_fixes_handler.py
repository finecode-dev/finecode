from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

from finecode_extension_api import code_action
from finecode_extension_api.actions.code_quality.get_lint_fixes_action import (
    GetLintFixesRunContext,
    GetLintFixesRunPayload,
    GetLintFixesRunResult,
)
from finecode_extension_api.actions.code_quality.get_lint_fixes_python_files_action import (
    GetLintFixesPythonFilesAction,
)
from finecode_extension_api.actions.code_quality.lint_fix import (
    FixApplicability,
    LintFix,
    Position,
    Range,
    TextEdit,
)
from finecode_extension_api.interfaces import icommandrunner, ifileeditor, ilogger, iprojectinfoprovider
from finecode_extension_api.resource_uri import ResourceUri, resource_uri_to_path
from fine_python_ruff.ruff_lsp_service import RuffLspService


@dataclasses.dataclass
class RuffGetLintFixesHandlerConfig(code_action.ActionHandlerConfig):
    line_length: int = 88
    target_version: str = "py38"
    select: list[str] | None = None
    ignore: list[str] | None = None
    extend_select: list[str] | None = None
    preview: bool = False
    use_cli: bool = False


class RuffGetLintFixesHandler(
    code_action.ActionHandler[
        GetLintFixesPythonFilesAction, RuffGetLintFixesHandlerConfig
    ]
):
    FILE_OPERATION_AUTHOR = ifileeditor.FileOperationAuthor(id="RuffGetLintFixesHandler")

    def __init__(
        self,
        config: RuffGetLintFixesHandlerConfig,
        logger: ilogger.ILogger,
        file_editor: ifileeditor.IFileEditor,
        command_runner: icommandrunner.ICommandRunner,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        lsp_service: RuffLspService,
    ) -> None:
        self.config = config
        self.logger = logger
        self.file_editor = file_editor
        self.command_runner = command_runner
        self.project_info_provider = project_info_provider
        self.lsp_service = lsp_service

        self.ruff_bin_path = Path(sys.executable).parent / "ruff"

    async def run(
        self,
        payload: GetLintFixesRunPayload,
        run_context: GetLintFixesRunContext,
    ) -> GetLintFixesRunResult:
        file_path = resource_uri_to_path(payload.file_path)

        async with self.file_editor.session(
            author=self.FILE_OPERATION_AUTHOR
        ) as session:
            async with session.read_file(file_path=file_path) as file_info:
                file_content: str = file_info.content
                file_version: str = file_info.version

        # Reject stale requests cheaply.
        if payload.file_version is not None and payload.file_version != file_version:
            return GetLintFixesRunResult(file_version=file_version, fixes=[])

        if self.config.use_cli:
            fixes = await self._run_cli_fixes(file_path, file_content, payload)
        else:
            fixes = await self._run_lsp_fixes(file_path, payload)

        return GetLintFixesRunResult(file_version=file_version, fixes=fixes)

    # ------------------------------------------------------------------
    # CLI path
    # ------------------------------------------------------------------

    async def _run_cli_fixes(
        self,
        file_path: Path,
        file_content: str,
        payload: GetLintFixesRunPayload,
    ) -> list[LintFix]:
        cmd = [
            str(self.ruff_bin_path),
            "check",
            "--output-format",
            "json",
            "--line-length",
            str(self.config.line_length),
            "--target-version",
            self.config.target_version,
            "--stdin-filename",
            str(file_path),
            "-",
        ]

        if self.config.select is not None:
            cmd.append("--select=" + ",".join(self.config.select))
        if self.config.extend_select is not None:
            cmd.append("--extend-select=" + ",".join(self.config.extend_select))
        if self.config.ignore is not None:
            cmd.append("--ignore=" + ",".join(self.config.ignore))
        if self.config.preview:
            cmd.append("--preview")

        ruff_process = await self.command_runner.run(cmd)
        ruff_process.write_to_stdin(file_content)
        ruff_process.close_stdin()
        await ruff_process.wait_for_end()

        output = ruff_process.get_output()
        try:
            violations = json.loads(output)
        except json.JSONDecodeError:
            raise code_action.ActionFailedException(
                f"ruff output is not valid JSON: {output}"
            )

        file_uri: ResourceUri = payload.file_path
        fixes: list[LintFix] = []
        fix_index = 0

        for violation in violations:
            raw_fix = violation.get("fix")
            if raw_fix is None:
                continue

            code: str = violation.get("code", "")
            location = violation.get("location", {})
            end_location = violation.get("end_location", {})

            target_range = Range(
                start=Position(
                    line=max(1, location.get("row", 1)) - 1,
                    character=max(0, location.get("column", 0)),
                ),
                end=Position(
                    line=max(1, end_location.get("row", 1)) - 1,
                    character=max(0, end_location.get("column", 0)),
                ),
            )

            # Filter by range when requested.
            if payload.range is not None and not _ranges_overlap(target_range, payload.range):
                continue

            # Filter by diagnostic codes when requested.
            if payload.diagnostic_codes is not None and code not in payload.diagnostic_codes:
                continue

            applicability_str = raw_fix.get("applicability", "safe")
            try:
                applicability = FixApplicability(applicability_str)
            except ValueError:
                applicability = FixApplicability.SAFE

            text_edits: list[TextEdit] = []
            for raw_edit in raw_fix.get("edits", []):
                edit_loc = raw_edit.get("location", {})
                edit_end = raw_edit.get("end_location", {})
                text_edits.append(
                    TextEdit(
                        range=Range(
                            start=Position(
                                line=max(1, edit_loc.get("row", 1)) - 1,
                                character=max(0, edit_loc.get("column", 0)),
                            ),
                            end=Position(
                                line=max(1, edit_end.get("row", 1)) - 1,
                                character=max(0, edit_end.get("column", 0)),
                            ),
                        ),
                        new_text=raw_edit.get("content", ""),
                    )
                )

            fix_id = f"ruff:{code}:{target_range.start.line}:{target_range.start.character}:{fix_index}"
            fix_index += 1
            title = raw_fix.get("message") or f"Fix {code}"
            is_safe = applicability == FixApplicability.SAFE

            fixes.append(
                LintFix(
                    fix_id=fix_id,
                    title=title,
                    kind="quickfix",
                    edits={file_uri: text_edits} if text_edits else {},
                    target_range=target_range,
                    target_codes=[code] if code else [],
                    is_preferred=is_safe,
                    applicability=applicability,
                )
            )

        return fixes

    # ------------------------------------------------------------------
    # LSP path
    # ------------------------------------------------------------------

    async def _run_lsp_fixes(
        self,
        file_path: Path,
        payload: GetLintFixesRunPayload,
    ) -> list[LintFix]:
        root_uri = self.project_info_provider.get_current_project_dir_path().as_uri()
        await self.lsp_service.ensure_started(root_uri)

        # Ensure the document is open and current before requesting code actions.
        await self.lsp_service.check_file(file_path)

        file_uri = file_path.as_uri()

        request_range = payload.range or Range(
            start=Position(line=0, character=0),
            end=Position(line=0, character=0),
        )

        context: dict[str, Any] = {
            "diagnostics": [],
        }
        if payload.kinds is not None:
            context["only"] = payload.kinds

        raw_actions = await self.lsp_service.request(
            "textDocument/codeAction",
            {
                "textDocument": {"uri": file_uri},
                "range": {
                    "start": {
                        "line": request_range.start.line,
                        "character": request_range.start.character,
                    },
                    "end": {
                        "line": request_range.end.line,
                        "character": request_range.end.character,
                    },
                },
                "context": context,
            },
        )

        if not raw_actions:
            return []

        return _map_lsp_code_actions_to_lint_fixes(
            raw_actions, file_uri, payload
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _ranges_overlap(a: Range, b: Range) -> bool:
    """Return True if ranges *a* and *b* overlap (share at least one position)."""
    a_start = (a.start.line, a.start.character)
    a_end = (a.end.line, a.end.character)
    b_start = (b.start.line, b.start.character)
    b_end = (b.end.line, b.end.character)
    return a_start < b_end and b_start < a_end


def _map_lsp_code_actions_to_lint_fixes(
    raw_actions: list[dict[str, Any]],
    file_uri: ResourceUri,
    payload: GetLintFixesRunPayload,
) -> list[LintFix]:
    fixes: list[LintFix] = []
    for i, action in enumerate(raw_actions):
        if not isinstance(action, dict):
            continue

        title: str = action.get("title", f"Fix {i}")
        kind: str = action.get("kind", "quickfix")

        # Filter by requested kinds.
        if payload.kinds is not None and not any(
            kind == k or kind.startswith(k + ".") for k in payload.kinds
        ):
            continue

        edits: dict[ResourceUri, list[TextEdit]] = {}
        workspace_edit = action.get("edit") or {}
        changes = workspace_edit.get("changes") or {}
        for uri, raw_edits in changes.items():
            edits[ResourceUri(uri)] = [
                TextEdit(
                    range=Range(
                        start=Position(
                            line=e["range"]["start"]["line"],
                            character=e["range"]["start"]["character"],
                        ),
                        end=Position(
                            line=e["range"]["end"]["line"],
                            character=e["range"]["end"]["character"],
                        ),
                    ),
                    new_text=e.get("newText", ""),
                )
                for e in raw_edits
            ]

        # Extract diagnostic codes from the action's diagnostics, if any.
        target_codes: list[str] = []
        for diag in action.get("diagnostics") or []:
            code = diag.get("code")
            if code:
                target_codes.append(str(code))

        # Determine target_range: use payload.range or default to start of file.
        target_range = payload.range or Range(
            start=Position(line=0, character=0),
            end=Position(line=0, character=0),
        )

        fix_id = f"ruff:lsp:{kind}:{i}"
        is_preferred = bool(action.get("isPreferred", False))

        fixes.append(
            LintFix(
                fix_id=fix_id,
                title=title,
                kind=kind,
                edits=edits,
                target_range=target_range,
                target_codes=target_codes,
                is_preferred=is_preferred,
                applicability=FixApplicability.SAFE if is_preferred else FixApplicability.UNSAFE,
            )
        )

    return fixes
