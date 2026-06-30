from finecode_extension_api import code_action
from fine_semantic_tokens.text_document_semantic_tokens_action import (
    TextDocumentSemanticTokensAction,
    SemanticTokensPayload,
    SemanticTokensResult,
)


class TextDocumentSemanticTokensTomlAction(code_action.Action):
    """Provide semantic tokens for a TOML source file."""

    DESCRIPTION = "Provide semantic tokens for a TOML source file."
    PAYLOAD_TYPE = SemanticTokensPayload
    RESULT_TYPE = SemanticTokensResult
    LANGUAGE = "toml"
    PARENT_ACTION = TextDocumentSemanticTokensAction
    HANDLER_EXECUTION = code_action.HandlerExecution.CONCURRENT
