# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.resource_uri import ResourceUri


@dataclasses.dataclass
class BuildDocsRunPayload(code_action.RunActionPayload):
    docs_source_dir: ResourceUri | None = None
    """``file://`` URI of the documentation source directory.
    None = infer from project tool config (e.g. mkdocs.yml, docs/conf.py)."""
    output_dir: ResourceUri | None = None
    """``file://`` URI of the directory where built documentation will be written.
    None = use the tool's default output location (e.g. site/, _build/html)."""


class BuildDocsRunContext(code_action.RunActionContext[BuildDocsRunPayload]):
    ...


@dataclasses.dataclass
class BuildDocsRunResult(code_action.RunActionResult):
    output_dir: ResourceUri | None = None
    """Directory containing the built documentation files."""

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, BuildDocsRunResult):
            return
        if other.output_dir is not None:
            self.output_dir = other.output_dir

    def to_text(self) -> str | textstyler.StyledText:
        if self.output_dir:
            return f"Documentation built at: {self.output_dir}"
        return "Documentation built."

    @property
    def return_code(self) -> code_action.RunReturnCode:
        return code_action.RunReturnCode.SUCCESS


class BuildDocsAction(
    code_action.Action[
        BuildDocsRunPayload,
        BuildDocsRunContext,
        BuildDocsRunResult,
    ]
):
    """Build documentation from source into a static output directory.

    Produces static HTML (or other output format) from documentation source files.
    The handler determines the tool and output format; tool-specific options belong
    in handler configuration.

    When docs_source_dir is None, the handler infers the source location from the
    project's tool configuration file (e.g. mkdocs.yml, docs/conf.py, book.toml).
    When output_dir is None, the handler uses the tool's default output location.
    """

    DESCRIPTION = "Build documentation from source into a static output directory."
    PAYLOAD_TYPE = BuildDocsRunPayload
    RUN_CONTEXT_TYPE = BuildDocsRunContext
    RESULT_TYPE = BuildDocsRunResult
