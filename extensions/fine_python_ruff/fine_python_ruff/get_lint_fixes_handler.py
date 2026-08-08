from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import sys
from pathlib import Path
from typing import Any

from fine_lint.get_lint_fixes_action import (
    GetLintFixesRunContext,
    GetLintFixesRunPayload,
    GetLintFixesRunResult,
)
from fine_lint.lint_fix import (
    FixApplicability,
    LintFix,
    Position,
    Range,
    TextEdit,
)
from fine_python_lang import support_range
from fine_python_lang.get_lint_fixes_python_files_action import (
    GetLintFixesPythonFilesAction,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    icommandrunner,
    ifileeditor,
    ilogger,
    iprojectactionrunner,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import ResourceUri, resource_uri_to_path

from fine_python_ruff import target_version as target_version_utils
from fine_python_ruff.ruff_lsp_service import RuffLspService

_MAX_CHARACTER = 2**31 - 1

# LSP counts lines by \n, \r\n and \r only. `str.splitlines` also breaks on
# several other control and Unicode separators (vertical tab, form feed, the C1
# NEL, the Unicode line/paragraph separators), which would make a file
# containing any of them look longer here than it does to the server -- and a
# whole-document range built from that count stops short of the real last line,
# hiding every diagnostic below the first such character.
_LINE_TERMINATOR_RE = re.compile(r"\r\n|\r|\n")
"""A character offset no line reaches, for ranges meant to run to end of line."""


@dataclasses.dataclass
class RuffGetLintFixesHandlerConfig(code_action.ActionHandlerConfig):
    line_length: int = 88
    target_version: str | None = None
    """Language level to compute fixes for, e.g. ``"py311"``.

    None derives it from the project's declared support range
    (``get_src_artifact_toolchain_range``)."""
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
    FILE_OPERATION_AUTHOR = ifileeditor.FileOperationAuthor(
        id="RuffGetLintFixesHandler"
    )

    def __init__(
        self,
        config: RuffGetLintFixesHandlerConfig,
        logger: ilogger.ILogger,
        file_editor: ifileeditor.IFileEditor,
        command_runner: icommandrunner.ICommandRunner,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        action_runner: iprojectactionrunner.IProjectActionRunner,
        lsp_service: RuffLspService,
    ) -> None:
        self.config = config
        self.logger = logger
        self.file_editor = file_editor
        self.command_runner = command_runner
        self.project_info_provider = project_info_provider
        self.lsp_service = lsp_service

        self.ruff_bin_path = Path(sys.executable).parent / "ruff"

        self._support_range_resolver = support_range.PythonSupportRangeResolver(
            action_runner=action_runner, logger=logger
        )
        self._target_version_resolved = False
        self._target_version_lock = asyncio.Lock()
        self._target_version: str | None = None

        if not self.config.use_cli:
            self.lsp_service.add_settings_provider(self._provide_lsp_settings)

    async def _ensure_target_version(self, meta: code_action.RunActionMeta) -> None:
        if self._target_version_resolved:
            return

        async with self._target_version_lock:
            if self._target_version_resolved:
                return

            self._target_version = await target_version_utils.resolve_target_version(
                configured=self.config.target_version,
                resolver=self._support_range_resolver,
                meta=meta,
                logger=self.logger,
            )
            self._target_version_resolved = True

    async def _provide_lsp_settings(
        self, meta: code_action.RunActionMeta
    ) -> dict[str, object]:
        """The language level, for the case where no other handler supplies one.

        Rule selection is deliberately not contributed here: fixes come from the same
        diagnostics the lint handler configures, and a second `lint` block would race
        with that one for a setting ruff reads once. The level is different -- a code
        action can be the first thing a session ever asks ruff for, and without this the
        server would be started with no language level at all.
        """
        await self._ensure_target_version(meta)

        if self._target_version is None:
            return {}
        return {"configuration": {"target-version": self._target_version}}

    async def run(
        self,
        payload: GetLintFixesRunPayload,
        run_context: GetLintFixesRunContext,
    ) -> GetLintFixesRunResult:
        if self.config.use_cli:
            # the LSP path derives it inside the settings provider instead, so that it
            # is in place before the shared server starts
            await self._ensure_target_version(run_context.meta)

        file_path = resource_uri_to_path(payload.file_path)

        async with self.file_editor.session(
            author=self.FILE_OPERATION_AUTHOR
        ) as session:
            async with session.read_file(file_path=file_path) as file_info:
                file_content: str = file_info.content
                file_version: str = file_info.version

        # The run context pins one base version for the whole run (design note D6),
        # so that concurrent handlers computing fixes for one file agree on the
        # content they are fixing. A mismatch here means the file changed between
        # the context's read and this handler's own read -- the same race the
        # payload-level staleness guard below exists to catch, so it is handled
        # identically: return no fixes rather than compute against content another
        # handler was not shown.
        if run_context.file_version != file_version:
            return GetLintFixesRunResult(file_version=file_version, fixes=[])

        # Reject stale requests cheaply.
        if payload.file_version is not None and payload.file_version != file_version:
            return GetLintFixesRunResult(file_version=file_version, fixes=[])

        if self.config.use_cli:
            fixes = await self._run_cli_fixes(file_path, file_content, payload)
        else:
            fixes = await self._run_lsp_fixes(
                file_path, file_content, payload, run_context.meta
            )

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
            "--stdin-filename",
            str(file_path),
            "-",
        ]

        # omitted when unknown so ruff infers it from requires-python
        if self._target_version is not None:
            cmd += ["--target-version", self._target_version]

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
        # Occurrences per semantic key, so that a diagnostic offering several fixes
        # gets distinguishable ids without an unrelated earlier violation's presence
        # or absence renumbering everything after it. Shared scheme with the LSP path
        # -- see _next_occurrence_fix_id.
        seen_keys: dict[str, int] = {}

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
            if payload.range is not None and not _ranges_overlap(
                target_range, payload.range
            ):
                continue

            # Filter by diagnostic codes when requested.
            if (
                payload.diagnostic_codes is not None
                and code not in payload.diagnostic_codes
            ):
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

            key = f"ruff:{code}:{target_range.start.line}:{target_range.start.character}"
            fix_id = _next_occurrence_fix_id(seen_keys, key)
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
        file_content: str,
        payload: GetLintFixesRunPayload,
        meta: code_action.RunActionMeta,
    ) -> list[LintFix]:
        root_uri = self.project_info_provider.get_current_project_dir_path().as_uri()
        await self.lsp_service.ensure_started(root_uri, meta)

        request_range = payload.range or _whole_document_range(file_content)

        # The service holds the document open across the whole interaction and
        # puts the file's own diagnostics into the request context; without both,
        # ruff has nothing to attach a per-diagnostic fix to and answers with its
        # blanket source actions at best.
        raw_actions = await self.lsp_service.get_code_actions(
            file_path,
            file_content,
            {
                "start": {
                    "line": request_range.start.line,
                    "character": request_range.start.character,
                },
                "end": {
                    "line": request_range.end.line,
                    "character": request_range.end.character,
                },
            },
            only=payload.kinds,
            diagnostic_codes=payload.diagnostic_codes,
        )

        if not raw_actions:
            return []

        return _map_lsp_code_actions_to_lint_fixes(raw_actions, payload, self.logger)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _whole_document_range(file_content: str) -> Range:
    """A range spanning the entire document, for requests with no explicit range.

    LSP servers only return code actions whose diagnostics overlap the requested
    range, so this has to actually reach the last line rather than being a
    zero-width placeholder at the start of the file.
    """
    # Always at least one element, empty content included -- an empty document
    # still has a line 0 for a diagnostic to sit on.
    lines = _LINE_TERMINATOR_RE.split(file_content)
    return Range(
        start=Position(line=0, character=0),
        # Saturating rather than len(lines[-1]): LSP character offsets are UTF-16
        # code units, so a last line with astral characters ends further along
        # than its Python length, and a diagnostic there would fall outside the
        # range. The spec requires servers to clamp an offset past the line end
        # back to the line end, which makes the exact value unimportant.
        end=Position(line=len(lines) - 1, character=_MAX_CHARACTER),
    )


def _ranges_overlap(a: Range, b: Range) -> bool:
    """Return True if ranges *a* and *b* overlap (share at least one position)."""
    a_start = (a.start.line, a.start.character)
    a_end = (a.end.line, a.end.character)
    b_start = (b.start.line, b.start.character)
    b_end = (b.end.line, b.end.character)
    return a_start < b_end and b_start < a_end


def _next_occurrence_fix_id(seen_keys: dict[str, int], key: str) -> str:
    """Build a deterministic ``fix_id`` for *key*, suffixed by its occurrence count.

    Shared by the CLI and LSP paths so their id schemes cannot drift apart again.
    ``key`` is expected to be semantic and position-based (e.g.
    ``ruff:{code}:{line}:{character}``), never index-based: a fix_id built from a
    counter incremented across the whole response changes meaning as soon as an
    unrelated fix is added or removed earlier in the file, which breaks resolve's
    contract that identical content re-derives the same id (design note D9).
    """
    occurrence = seen_keys.get(key, 0)
    seen_keys[key] = occurrence + 1
    return f"{key}:{occurrence}"


def _map_lsp_code_actions_to_lint_fixes(
    raw_actions: list[dict[str, Any]],
    payload: GetLintFixesRunPayload,
    logger: ilogger.ILogger,
) -> list[LintFix]:
    fixes: list[LintFix] = []
    # Occurrences per semantic key, so that a diagnostic offering several fixes
    # ("remove the import" / "add a noqa") gets distinguishable ids without
    # position in the response deciding what they are.
    seen_keys: dict[str, int] = {}
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

        # Extract diagnostic codes and range from the action's diagnostics, if any.
        diagnostics = action.get("diagnostics") or []
        target_codes: list[str] = []
        for diag in diagnostics:
            code = diag.get("code")
            if code:
                target_codes.append(str(code))

        # Prefer the diagnostic's own range -- it identifies which error this fix
        # addresses. Falling back to the query range would make every fix from a
        # whole-file request look identical and unattributable to a diagnostic.
        first_diag_range = diagnostics[0].get("range") if diagnostics else None
        if first_diag_range:
            target_range = Range(
                start=Position(
                    line=first_diag_range["start"]["line"],
                    character=first_diag_range["start"]["character"],
                ),
                end=Position(
                    line=first_diag_range["end"]["line"],
                    character=first_diag_range["end"]["character"],
                ),
            )
        else:
            target_range = payload.range or Range(
                start=Position(line=0, character=0),
                end=Position(line=0, character=0),
            )

        # Stable across requests: fix_id is the codeAction/resolve key, so an id
        # built from the action's index changes meaning as soon as an unrelated
        # edit adds or removes a fix earlier in the file.
        if target_codes:
            key = (
                f"ruff:{target_codes[0]}"
                f":{target_range.start.line}:{target_range.start.character}"
            )
        else:
            key = f"ruff:{kind}"
        fix_id = _next_occurrence_fix_id(seen_keys, key)

        if kind == "quickfix" and not edits:
            # Not fatal, and not something a caller can tell apart from a
            # display-only fix, so say it here: it is what a server switching to
            # resolve-deferred edits looks like from the outside.
            logger.warning(
                f"ruff returned quickfix {title!r} with no edit; it will be offered"
                " but will change nothing"
            )

        fixes.append(
            LintFix(
                fix_id=fix_id,
                title=title,
                kind=kind,
                edits=edits,
                target_range=target_range,
                target_codes=target_codes,
                # LSP carries no applicability signal, and ruff offers unsafe
                # fixes over LSP only when configured to -- so everything that
                # arrives here is applicable. isPreferred is the "highlight this
                # one in the menu" flag and says nothing about safety; reading it
                # as such marked every non-highlighted fix unsafe. Callers that
                # need ruff's real applicability want the CLI path, which has it.
                applicability=FixApplicability.SAFE,
                is_preferred=bool(action.get("isPreferred", False)),
            )
        )

    return fixes
