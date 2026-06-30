from __future__ import annotations

import dataclasses
from typing import Any

from finecode_extension_api import code_action, common_types
from finecode_extension_api.resource_uri import ResourceUri


SEMANTIC_TOKEN_TYPES: list[str] = [
    "namespace",     # 0
    "type",          # 1
    "class",         # 2
    "enum",          # 3
    "interface",     # 4
    "struct",        # 5
    "typeParameter", # 6
    "parameter",     # 7
    "variable",      # 8
    "property",      # 9
    "enumMember",    # 10
    "event",         # 11
    "function",      # 12
    "method",        # 13
    "macro",         # 14
    "keyword",       # 15
    "modifier",      # 16
    "comment",       # 17
    "string",        # 18
    "number",        # 19
    "regexp",        # 20
    "operator",      # 21
    "decorator",     # 22
]

SEMANTIC_TOKEN_MODIFIERS: list[str] = [
    "declaration",    # bit 0
    "definition",     # bit 1
    "readonly",       # bit 2
    "static",         # bit 3
    "deprecated",     # bit 4
    "abstract",       # bit 5
    "async",          # bit 6
    "modification",   # bit 7
    "documentation",  # bit 8
    "defaultLibrary", # bit 9
]


@dataclasses.dataclass
class SemanticToken:
    """A single semantic token at an absolute position in the document.

    All positions are 0-based and absolute (not delta-encoded). The LSP wire
    encoding is produced by the LSP endpoint after merging all handler results,
    not by handlers themselves.

    token_type_index and token_modifiers_bitmask must use the indices from the
    legend declared in the server's ``initialize`` response
    (``SemanticTokensLegend``).
    """

    line: int
    """0-based absolute line number."""
    char: int
    """0-based absolute character offset within the line."""
    length: int
    """Token length in characters."""
    token_type_index: int
    """Index into the server's SemanticTokensLegend.tokenTypes array."""
    token_modifiers_bitmask: int
    """Bitmask of active modifiers from SemanticTokensLegend.tokenModifiers."""


@dataclasses.dataclass
class SemanticTokensPayload(code_action.RunActionPayload):
    uri: ResourceUri
    """The document to tokenize."""
    range: common_types.Range | None = None
    """If set, return tokens only within this range (semanticTokens/range).
    If None, return tokens for the full document (semanticTokens/full)."""


@dataclasses.dataclass
class SemanticTokensResult(code_action.RunActionResult):
    """Accumulated semantic tokens for a single document at absolute positions.

    Tokens are stored at absolute positions. The LSP endpoint is responsible
    for sorting by (line, char) and delta-encoding before sending the wire
    response.

    result_id, when set, enables the delta protocol: subsequent requests may
    use TextDocumentSemanticTokensDeltaAction instead of a full re-request.
    Handlers that do not support incremental computation must leave result_id
    as None.
    """

    tokens: list[SemanticToken] = dataclasses.field(default_factory=list)
    """Semantic tokens at absolute positions. Empty list means the handler
    ran and found no tokens."""
    result_id: str | None = None
    """Opaque identifier for this result snapshot, used by the delta protocol.
    None = this handler does not participate in incremental updates."""

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, SemanticTokensResult):
            return
        # Safe to extend regardless of order: absolute positions are independent,
        # sorting happens once at the endpoint before wire encoding.
        self.tokens.extend(other.tokens)
        if other.result_id is not None:
            self.result_id = other.result_id


def decode_lsp_semantic_tokens(
    data: list[int],
    server_types: list[str],
    server_modifiers: list[str],
) -> list[SemanticToken]:
    """Decode LSP delta-encoded semantic token data into absolute-position SemanticToken objects.

    Maps server-local token type/modifier indices to the global indices defined in
    SEMANTIC_TOKEN_TYPES / SEMANTIC_TOKEN_MODIFIERS. Unknown types are silently dropped;
    unknown modifiers are silently ignored.
    """
    type_map: dict[int, int] = {}
    for i, t in enumerate(server_types):
        try:
            type_map[i] = SEMANTIC_TOKEN_TYPES.index(t)
        except ValueError:
            pass

    mod_bit_map: dict[int, int] = {}
    for i, m in enumerate(server_modifiers):
        try:
            mod_bit_map[i] = SEMANTIC_TOKEN_MODIFIERS.index(m)
        except ValueError:
            pass

    tokens: list[SemanticToken] = []
    line, char = 0, 0

    for i in range(0, len(data), 5):
        if i + 5 > len(data):
            break
        delta_line = data[i]
        delta_char = data[i + 1]
        length = data[i + 2]
        server_type_idx = data[i + 3]
        server_modifiers_bits = data[i + 4]

        if delta_line > 0:
            line += delta_line
            char = delta_char
        else:
            char += delta_char

        global_type_idx = type_map.get(server_type_idx)
        if global_type_idx is None:
            continue

        global_modifiers = 0
        for server_bit, global_bit in mod_bit_map.items():
            if server_modifiers_bits & (1 << server_bit):
                global_modifiers |= (1 << global_bit)

        tokens.append(
            SemanticToken(
                line=line,
                char=char,
                length=length,
                token_type_index=global_type_idx,
                token_modifiers_bitmask=global_modifiers,
            )
        )

    return tokens


class TextDocumentSemanticTokensAction(code_action.Action):
    """Classify tokens in a text document for semantic syntax highlighting.

    Covers both full-document tokenization (range=None) and range-restricted
    tokenization (range set). Returns tokens as absolute-position SemanticToken
    objects; the LSP endpoint sorts and encodes them into the wire format.

    Handlers run concurrently — each contributes tokens for its own token types
    without depending on other handlers. An empty tokens list is not an error.

    Use language-specific subactions (e.g. TextDocumentSemanticTokensPythonAction)
    to restrict handlers to a particular language. A dispatch handler on this
    generic action routes each document to the matching language subaction.
    """

    DESCRIPTION = "Classify tokens in a text document for semantic syntax highlighting."
    PAYLOAD_TYPE = SemanticTokensPayload
    RESULT_TYPE = SemanticTokensResult
    HANDLER_EXECUTION = code_action.HandlerExecution.CONCURRENT
