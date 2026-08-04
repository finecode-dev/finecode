# docs: docs/reference/actions.md
import dataclasses
import sys

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from fine_envs.env_inventory import EnvEntry, EnvState
from finecode_extension_api import code_action, textstyler


@dataclasses.dataclass
class ListEnvsRunPayload(code_action.RunActionPayload): ...


class ListEnvsRunContext(code_action.RunActionContext[ListEnvsRunPayload]): ...


@dataclasses.dataclass
class ListEnvsRunResult(code_action.RunActionResult):
    envs: list[EnvEntry] = dataclasses.field(default_factory=list)
    """Every env of the project — declared ones first, then orphans."""

    @override
    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, ListEnvsRunResult):
            return

        known = {env.name for env in self.envs}
        for env in other.envs:
            if env.name not in known:
                self.envs.append(env)
                known.add(env.name)

    @override
    def to_text(self) -> str | textstyler.StyledText:
        text = textstyler.StyledText()
        if not self.envs:
            text.append("No environments.\n")
            return text

        name_width = max(len(env.name) for env in self.envs)
        for env in self.envs:
            text.append(f"  {env.name.ljust(name_width)}  ")
            if env.orphaned:
                text.append_styled("ORPHANED", foreground=textstyler.Color.YELLOW)
            else:
                text.append("declared")
            if env.state is EnvState.MISSING:
                text.append("  MISSING\n")
            elif env.state is EnvState.BROKEN:
                text.append("  ")
                text.append_styled("BROKEN", foreground=textstyler.Color.RED)
                text.append("\n")
            else:
                text.append("  created\n")

        return text

    @property
    @override
    def return_code(self) -> code_action.RunReturnCode:
        # Reporting orphaned or broken envs is the job, not a failure.
        return code_action.RunReturnCode.SUCCESS


class ListEnvsAction(
    code_action.Action[ListEnvsRunPayload, ListEnvsRunContext, ListEnvsRunResult]
):
    """List the project's environments with their config and on-disk status.

    State is derived from the filesystem alone — no interpreter is executed —
    so listing stays cheap across a whole workspace. A `CREATED` env is
    therefore "looks like a venv", not "verified runnable".
    """

    DESCRIPTION = (
        "List the project's environments, showing which are declared in"
        " configuration, which exist on disk, and which are orphaned."
    )
    PAYLOAD_TYPE = ListEnvsRunPayload
    RUN_CONTEXT_TYPE = ListEnvsRunContext
    RESULT_TYPE = ListEnvsRunResult
