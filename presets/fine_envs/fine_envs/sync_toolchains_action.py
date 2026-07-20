# docs: docs/reference/actions.md
import dataclasses
import sys

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.resource_uri import ResourceUri


@dataclasses.dataclass
class SyncToolchainsRunPayload(code_action.RunActionPayload):
    project_def_path: ResourceUri | None = None
    """``file://`` URI of the project definition file declaring the envs (e.g. pyproject.toml).

    None means the current project's definition file."""
    save: bool = True
    """Whether to write the derived axis back to the project definition file.

    False derives and reports the axis without touching the file."""


class SyncToolchainsRunContext(
    code_action.RunActionContext[SyncToolchainsRunPayload]
): ...


@dataclasses.dataclass
class EnvToolchainAxis:
    """The declared and derived toolchain axis of one env."""

    env_name: str
    declared: list[str] = dataclasses.field(default_factory=list)
    """Canonical toolchain identities already written in the config.

    Empty when the env declares no axis yet. Identity format is the language's
    own — in Python, ``<implementation>@<version>``."""
    derived: list[str] = dataclasses.field(default_factory=list)
    """Canonical identities the language source computed, in canonical order."""

    @property
    def changed(self) -> bool:
        """Whether the declared axis differs from the derived one.

        The axis is wholly generated, so order is part of the contract: a
        reordered axis is drift and gets normalized by the next sync.
        """
        return self.declared != self.derived


@dataclasses.dataclass
class SyncToolchainsRunResult(code_action.RunActionResult):
    axes: list[EnvToolchainAxis] = dataclasses.field(default_factory=list)
    saved: bool = False
    """Whether a derived axis was written to the project definition file."""

    @override
    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, SyncToolchainsRunResult):
            return

        known_envs = {axis.env_name for axis in self.axes}
        for axis in other.axes:
            if axis.env_name not in known_envs:
                self.axes.append(axis)
                known_envs.add(axis.env_name)
        self.saved = self.saved or other.saved

    @override
    def to_text(self) -> str | textstyler.StyledText:
        text = textstyler.StyledText()
        if not self.axes:
            text.append("No env declares a derived toolchain axis.\n")
            return text

        for axis in self.axes:
            text.append_styled(axis.env_name, bold=True)
            text.append(f": {', '.join(axis.derived)}")
            if axis.changed:
                text.append(" (updated)" if self.saved else " (out of date)")
            text.append("\n")
        return text

    @property
    @override
    def return_code(self) -> code_action.RunReturnCode:
        return code_action.RunReturnCode.SUCCESS


class SyncToolchainsAction(
    code_action.Action[
        SyncToolchainsRunPayload,
        SyncToolchainsRunContext,
        SyncToolchainsRunResult,
    ]
):
    """Derive each env's toolchain axis from the project's declared support range.

    Every ecosystem declares the versions it supports somewhere — ``requires-python``
    in Python, ``engines`` in Node, ``required_ruby_version`` in Ruby. A language
    handler expands that range into toolchain identities and materializes them into
    the project definition file, so config resolution stays a pure read (ADR-0053).

    The axis is *wholly* generated: additional toolchains are configured as inputs to
    the source, never hand-added to its output, which keeps regeneration idempotent
    and makes drift unambiguous.
    """

    DESCRIPTION = "Derive each env's toolchain axis from the project's declared support range."
    PAYLOAD_TYPE = SyncToolchainsRunPayload
    RUN_CONTEXT_TYPE = SyncToolchainsRunContext
    RESULT_TYPE = SyncToolchainsRunResult
