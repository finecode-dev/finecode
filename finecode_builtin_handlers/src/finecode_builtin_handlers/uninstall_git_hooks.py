# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.system import uninstall_git_hooks_action
from finecode_extension_api.interfaces import ilogger, iprojectinfoprovider
from finecode_builtin_handlers.git_hooks_common import (
    FINECODE_HOOK_MARKER,
    resolve_project_git_dir,
)


@dataclasses.dataclass
class UninstallGitHooksHandlerConfig(code_action.ActionHandlerConfig): ...


class UninstallGitHooksHandler(
    code_action.ActionHandler[
        uninstall_git_hooks_action.UninstallGitHooksAction,
        UninstallGitHooksHandlerConfig,
    ]
):
    """Remove FineCode-managed git hook scripts from .git/hooks/.

    Refuses to delete hook files that do not contain the FineCode marker comment.
    """

    def __init__(
        self,
        logger: ilogger.ILogger,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
    ) -> None:
        self.logger = logger
        self.project_info_provider = project_info_provider

    async def run(
        self,
        payload: uninstall_git_hooks_action.UninstallGitHooksRunPayload,
        run_context: uninstall_git_hooks_action.UninstallGitHooksRunContext,
    ) -> uninstall_git_hooks_action.UninstallGitHooksRunResult:
        project_dir = self.project_info_provider.get_current_project_dir_path()
        git_dir = resolve_project_git_dir(project_dir)
        if git_dir is None:
            reason = f"{project_dir} is not a git repository root"
            self.logger.info(
                f"Skipping git hook uninstallation: {reason}."
            )
            return uninstall_git_hooks_action.UninstallGitHooksRunResult(
                skip_reason=reason,
            )

        hooks_dir = git_dir / "hooks"

        removed: list[str] = []
        skipped: list[str] = []
        refused: list[str] = []

        for hook_type in payload.hook_types:
            hook_path = hooks_dir / hook_type
            if not hook_path.exists():
                self.logger.info(f"Hook '{hook_type}' not found — already uninstalled.")
                skipped.append(hook_type)
                continue

            content = hook_path.read_text(encoding="utf-8", errors="replace")
            if FINECODE_HOOK_MARKER not in content:
                self.logger.warning(
                    f"Hook '{hook_type}' at {hook_path} was not installed by FineCode "
                    "(missing marker). Refusing to delete."
                )
                refused.append(hook_type)
                continue

            hook_path.unlink()
            self.logger.info(f"Removed '{hook_type}' hook from {hook_path}.")
            removed.append(hook_type)

        return uninstall_git_hooks_action.UninstallGitHooksRunResult(
            removed_hooks=removed,
            skipped_hooks=skipped,
            refused_hooks=refused,
        )
