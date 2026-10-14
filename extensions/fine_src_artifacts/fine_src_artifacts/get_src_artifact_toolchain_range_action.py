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
class GetSrcArtifactToolchainRangeRunPayload(code_action.RunActionPayload):
    src_artifact_def_path: ResourceUri | None = None
    """``file://`` URI of the artifact's definition file (e.g. pyproject.toml).

    None means the current project's definition file."""


class GetSrcArtifactToolchainRangeRunContext(
    code_action.RunActionContext[GetSrcArtifactToolchainRangeRunPayload]
): ...


@dataclasses.dataclass
class GetSrcArtifactToolchainRangeRunResult(code_action.RunActionResult):
    min_version: str | None = None
    """Oldest toolchain version the artifact declares support for, e.g. ``3.11``.

    None means the artifact declares no support range at all -- consumers have nothing
    to derive from and fall back to their own defaults."""
    max_version: str | None = None
    """Newest supported toolchain version, or None when the declaration leaves the
    upper end open.

    Open is the normal case for a published package and is not a missing answer: it is
    what the project promised. A consumer needing a concrete ceiling has to get it from
    something that knows which versions exist -- the interpreter axis
    (``sync_toolchains``), not this action."""
    derived_from: str | None = None
    """Human-readable provenance of the range, for diagnosing what a tool targets.

    The point of deriving the range centrally is that a linter's target version stops
    being an unexplained default; this field is what makes the derivation answerable
    without reading handler source."""

    @override
    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, GetSrcArtifactToolchainRangeRunResult):
            return

        # Contributions intersect: merging may narrow a declared range but never widen
        # one, so registering an extra handler is always safe -- a second opinion can
        # add a constraint the first did not know about, never drop one it stated.
        contributed = False
        if other.min_version is not None and (
            self.min_version is None
            or _version_key(other.min_version) > _version_key(self.min_version)
        ):
            self.min_version = other.min_version
            contributed = True
        if other.max_version is not None and (
            self.max_version is None
            or _version_key(other.max_version) < _version_key(self.max_version)
        ):
            self.max_version = other.max_version
            contributed = True

        # Every handler whose bound survived is named, because the two ends can come
        # from different ones. Keeping only the first contributor's provenance instead
        # would attribute the reported range to a handler that decided neither end of
        # it, which is precisely what this field exists to prevent.
        if contributed and other.derived_from is not None:
            if self.derived_from is None:
                self.derived_from = other.derived_from
            elif other.derived_from not in self.derived_from:
                self.derived_from = f"{self.derived_from}; {other.derived_from}"

    @override
    def to_text(self) -> str | textstyler.StyledText:
        text = textstyler.StyledText()
        if self.min_version is None and self.max_version is None:
            text.append("No toolchain support range declared.\n")
            return text

        text.append_styled(self.min_version or "(unbounded)", bold=True)
        text.append(" .. ")
        text.append_styled(self.max_version or "(open)", bold=True)
        text.append("\n")
        if self.derived_from is not None:
            text.append(f"derived from: {self.derived_from}\n")
        return text

    @property
    @override
    def return_code(self) -> code_action.RunReturnCode:
        return code_action.RunReturnCode.SUCCESS


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) if part.isdigit() else 0 for part in version.split("."))


class GetSrcArtifactToolchainRangeAction(
    code_action.Action[
        GetSrcArtifactToolchainRangeRunPayload,
        GetSrcArtifactToolchainRangeRunContext,
        GetSrcArtifactToolchainRangeRunResult,
    ]
):
    """Read the range of toolchain versions a source artifact declares support for.

    Every ecosystem states this somewhere in its definition file -- ``requires-python``
    in Python, ``engines`` in Node, ``rust-version`` in Rust -- and every tool that
    compiles, formats, or lints against a language level needs it: ruff's
    ``target-version``, black's ``--target-version``, isort's ``py_version``. Left to
    themselves, each tool either re-derives the range with its own rules or takes a
    hardcoded default, and the two disagree silently. This action is the one place the
    range is read, so tools are configured from a single answer that a user can print,
    override per project, or replace outright by swapping the handler.

    It is deliberately *not* the interpreter axis (``sync_toolchains``). The axis is the
    declared range intersected with what the environment provisioner can obtain -- what
    the project is *tested on*. This is what the project *promises*, and the promise is
    what a language level has to encode: a tool targeting the axis floor stops reporting
    syntax that breaks the oldest interpreter users were told they could run.

    Holes in a declaration (``!=3.12.*``) are not represented; only its ends are.
    """

    DESCRIPTION = (
        "Read the range of toolchain versions a source artifact supports"
        " (e.g. Python 3.11 .. open, from requires-python)."
    )
    PAYLOAD_TYPE = GetSrcArtifactToolchainRangeRunPayload
    RUN_CONTEXT_TYPE = GetSrcArtifactToolchainRangeRunContext
    RESULT_TYPE = GetSrcArtifactToolchainRangeRunResult
