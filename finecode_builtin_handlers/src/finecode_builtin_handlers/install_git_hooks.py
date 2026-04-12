# docs: docs/reference/actions.md
import dataclasses
import stat
import sys

from finecode_extension_api import code_action
from finecode_extension_api.actions.system import install_git_hooks_action
from finecode_extension_api.interfaces import ilogger, iprojectinfoprovider
from finecode_builtin_handlers.git_hooks_common import (
    FINECODE_HOOK_MARKER,
    resolve_project_git_dir,
)

_HOOK_TEMPLATE = """\
#!/usr/bin/env python3
{hook_marker}
\"\"\"Git {hook_type} hook installed by FineCode.

To uninstall: python -m finecode run uninstall_git_hooks
To reinstall: python -m finecode run install_git_hooks
\"\"\"
import subprocess
import sys
from pathlib import Path

_python = Path(".venvs") / "dev_workspace" / (
    "Scripts\\\\python.exe" if sys.platform == "win32" else "bin/python"
)
if not _python.exists():
    print(
        f"FineCode: dev_workspace environment not found ({{_python}}).\\n"
        "Run 'python -m finecode prepare-envs' to set up environments.",
        file=sys.stderr,
    )
    sys.exit(1)
result = subprocess.run(
    [str(_python), "-m", "finecode", "run", "--dev-env=git_hook", "precommit"],
)
sys.exit(result.returncode)
"""


@dataclasses.dataclass
class InstallGitHooksHandlerConfig(code_action.ActionHandlerConfig): ...


class InstallGitHooksHandler(
    code_action.ActionHandler[
        install_git_hooks_action.InstallGitHooksAction, InstallGitHooksHandlerConfig
    ]
):
    """Install FineCode-managed git hook scripts into .git/hooks/."""

    def __init__(
        self,
        logger: ilogger.ILogger,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
    ) -> None:
        self.logger = logger
        self.project_info_provider = project_info_provider

    async def run(
        self,
        payload: install_git_hooks_action.InstallGitHooksRunPayload,
        run_context: install_git_hooks_action.InstallGitHooksRunContext,
    ) -> install_git_hooks_action.InstallGitHooksRunResult:
        project_dir = self.project_info_provider.get_current_project_dir_path()
        git_dir = resolve_project_git_dir(project_dir)
        if git_dir is None:
            reason = f"{project_dir} is not a git repository root"
            self.logger.info(
                f"Skipping git hook installation: {reason}."
            )
            return install_git_hooks_action.InstallGitHooksRunResult(
                skip_reason=reason,
            )

        hooks_dir = git_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)

        installed: list[str] = []
        skipped: list[str] = []

        for hook_type in payload.hook_types:
            hook_path = hooks_dir / hook_type
            if hook_path.exists() and not payload.force:
                self.logger.warning(
                    f"Hook '{hook_type}' already exists at {hook_path}. "
                    "Use force=True to overwrite."
                )
                skipped.append(hook_type)
                continue

            hook_content = _HOOK_TEMPLATE.format(
                hook_type=hook_type,
                hook_marker=FINECODE_HOOK_MARKER,
            )
            hook_path.write_text(hook_content, encoding="utf-8")

            # Make executable on POSIX (git on Windows handles permissions separately)
            if sys.platform != "win32":
                current_mode = hook_path.stat().st_mode
                hook_path.chmod(
                    current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
                )

            self.logger.info(f"Installed '{hook_type}' hook at {hook_path}.")
            installed.append(hook_type)

        return install_git_hooks_action.InstallGitHooksRunResult(
            installed_hooks=installed,
            skipped_hooks=skipped,
        )
