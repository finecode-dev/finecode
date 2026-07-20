# docs: docs/reference/actions.md
import dataclasses
import sys

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from finecode_extension_api import code_action, textstyler


@dataclasses.dataclass
class ListObtainableToolchainsRunPayload(code_action.RunActionPayload):
    include_prereleases: bool = False
    """Whether to include prerelease toolchains (e.g. a Python beta)."""


class ListObtainableToolchainsRunContext(
    code_action.RunActionContext[ListObtainableToolchainsRunPayload]
): ...


@dataclasses.dataclass
class ListObtainableToolchainsRunResult(code_action.RunActionResult):
    toolchains: list[str] = dataclasses.field(default_factory=list)
    """Canonical toolchain identities the provisioner can obtain, e.g. ``cpython@3.13``.

    Reduced to the granularity a matrix axis is declared at: no patch level, no build
    variant, no platform tag."""

    @override
    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, ListObtainableToolchainsRunResult):
            return

        known = set(self.toolchains)
        for toolchain in other.toolchains:
            if toolchain not in known:
                self.toolchains.append(toolchain)
                known.add(toolchain)

    @override
    def to_text(self) -> str | textstyler.StyledText:
        text = textstyler.StyledText()
        if not self.toolchains:
            text.append("No obtainable toolchains.\n")
            return text
        for toolchain in self.toolchains:
            text.append(f"{toolchain}\n")
        return text

    @property
    @override
    def return_code(self) -> code_action.RunReturnCode:
        return code_action.RunReturnCode.SUCCESS


class ListObtainableToolchainsAction(
    code_action.Action[
        ListObtainableToolchainsRunPayload,
        ListObtainableToolchainsRunContext,
        ListObtainableToolchainsRunResult,
    ]
):
    """List the toolchains the environment provisioner is able to obtain.

    "Obtainable" is deliberately not "installed". This action answers what the
    *provisioning toolchain* can get — a property of a locked dependency — and not
    what happens to be present on this machine. Only the former may feed a derived
    matrix axis: an axis sourced from local installs would differ between developers
    on the same commit. Asking whether a toolchain is available *here* is a separate
    question and belongs to a separate action (ADR-0053).

    The provisioner is the authority because deriving a version it cannot obtain
    yields an axis whose environments cannot be created.
    """

    DESCRIPTION = "List the toolchains the environment provisioner can obtain."
    PAYLOAD_TYPE = ListObtainableToolchainsRunPayload
    RUN_CONTEXT_TYPE = ListObtainableToolchainsRunContext
    RESULT_TYPE = ListObtainableToolchainsRunResult
