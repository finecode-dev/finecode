# docs: docs/reference/actions.md
import dataclasses
import sys

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.resource_uri import ResourceUri
from fine_envs.sync_toolchains_action import EnvToolchainAxis


@dataclasses.dataclass
class CheckToolchainsRunPayload(code_action.RunActionPayload):
    project_def_path: ResourceUri | None = None
    """``file://`` URI of the project definition file declaring the envs (e.g. pyproject.toml).

    None means the current project's definition file."""


class CheckToolchainsRunContext(
    code_action.RunActionContext[CheckToolchainsRunPayload]
): ...


@dataclasses.dataclass
class CheckToolchainsRunResult(code_action.RunActionResult):
    """Envs whose materialized toolchain axis no longer matches the derived one."""

    stale_axes: list[EnvToolchainAxis] = dataclasses.field(default_factory=list)

    @override
    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, CheckToolchainsRunResult):
            return

        known_envs = {axis.env_name for axis in self.stale_axes}
        for axis in other.stale_axes:
            if axis.env_name not in known_envs:
                self.stale_axes.append(axis)
                known_envs.add(axis.env_name)

    @override
    def to_text(self) -> str | textstyler.StyledText:
        text = textstyler.StyledText()
        if not self.stale_axes:
            text.append("Toolchain axes are up to date.\n")
            return text

        for axis in self.stale_axes:
            text.append_styled(axis.env_name, bold=True)
            text.append(": toolchain axis is out of date\n")
            text.append(f"  declared: {', '.join(axis.declared) or '(none)'}\n")
            text.append(f"  derived:  {', '.join(axis.derived) or '(none)'}\n")
        text.append("\nRun `sync_toolchains` to update.\n")
        return text

    @property
    @override
    def return_code(self) -> code_action.RunReturnCode:
        if self.stale_axes:
            return code_action.RunReturnCode.ERROR
        return code_action.RunReturnCode.SUCCESS


class CheckToolchainsAction(
    code_action.Action[
        CheckToolchainsRunPayload,
        CheckToolchainsRunContext,
        CheckToolchainsRunResult,
    ]
):
    """Check whether each env's materialized toolchain axis is still what the source derives.

    The axis is generated and committed, so it can go stale — the support range
    changes, or the source learns about a newer toolchain. This is the same staleness
    a lock file has, and it is caught the same way: by re-deriving and comparing in
    precommit and CI. Fails with a non-zero return code on drift.
    """

    DESCRIPTION = "Check whether each env's toolchain axis matches what the source derives."
    PAYLOAD_TYPE = CheckToolchainsRunPayload
    RUN_CONTEXT_TYPE = CheckToolchainsRunContext
    RESULT_TYPE = CheckToolchainsRunResult
