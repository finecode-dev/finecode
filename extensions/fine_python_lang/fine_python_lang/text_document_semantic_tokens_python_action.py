from finecode_extension_api import code_action
from fine_semantic_tokens.text_document_semantic_tokens_action import (
    TextDocumentSemanticTokensAction,
    SemanticTokensPayload,
    SemanticTokensResult,
)


class TextDocumentSemanticTokensPythonAction(code_action.Action):
    """Provide semantic tokens for a Python source file."""

    DESCRIPTION = "Provide semantic tokens for a Python source file."
    PAYLOAD_TYPE = SemanticTokensPayload
    RESULT_TYPE = SemanticTokensResult
    LANGUAGE = "python"
    PARENT_ACTION = TextDocumentSemanticTokensAction
    HANDLER_EXECUTION = code_action.HandlerExecution.CONCURRENT
