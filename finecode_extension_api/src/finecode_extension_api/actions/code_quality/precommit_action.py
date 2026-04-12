# docs: docs/reference/actions.md
import dataclasses
import sys
from pathlib import Path

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from finecode_extension_api import code_action


@dataclasses.dataclass
class PrecommitRunPayload(code_action.RunActionPayload):
    file_paths: list[Path] | None = None
    """Explicit file list.
    None = auto-detect staged files from git (done by discovery handler).
    [] = explicit no-op (nothing to process)."""


@dataclasses.dataclass
class PrecommitRunResult(code_action.RunActionResult):
    action_results: dict[str, code_action.RunActionResult] = dataclasses.field(
        default_factory=dict
    )

    @override
    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, PrecommitRunResult):
            return
        self.action_results.update(other.action_results)

    @property
    def return_code(self) -> code_action.RunReturnCode:
        if any(
            r.return_code != code_action.RunReturnCode.SUCCESS
            for r in self.action_results.values()
        ):
            return code_action.RunReturnCode.ERROR
        return code_action.RunReturnCode.SUCCESS


class PrecommitRunContext(code_action.RunActionContext[PrecommitRunPayload]):
    def __init__(
        self,
        run_id: int,
        initial_payload: PrecommitRunPayload,
        meta: code_action.RunActionMeta,
        info_provider: code_action.RunContextInfoProvider,
    ) -> None:
        super().__init__(
            run_id=run_id,
            initial_payload=initial_payload,
            meta=meta,
            info_provider=info_provider,
        )
        self.staged_files: list[Path] | None = None
        """Populated by discovery handler. None means discovery has not run yet.
        [] means discovery ran and found no staged files — bridge handlers must treat this as a no-op.
        Bridge handlers must raise if this is None (i.e. discovery handler was not registered or ran after them)."""


class PrecommitAction(
    code_action.Action[PrecommitRunPayload, PrecommitRunContext, PrecommitRunResult]
):
    """Run configured code quality checks on git-staged files before commit."""

    PAYLOAD_TYPE = PrecommitRunPayload
    RUN_CONTEXT_TYPE = PrecommitRunContext
    RESULT_TYPE = PrecommitRunResult
