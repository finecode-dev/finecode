from finecode_extension_api import code_action
from fine_inlay_hints.text_document_inlay_hint import (
    TextDocumentInlayHintAction,
    InlayHintPayload,
    InlayHintResult,
)


class TextDocumentInlayHintPythonAction(code_action.Action):
    """Provide inlay hints for a Python source file."""

    DESCRIPTION = "Provide inlay hints for a Python source file."
    PAYLOAD_TYPE = InlayHintPayload
    RESULT_TYPE = InlayHintResult
    LANGUAGE = "python"
    PARENT_ACTION = TextDocumentInlayHintAction
