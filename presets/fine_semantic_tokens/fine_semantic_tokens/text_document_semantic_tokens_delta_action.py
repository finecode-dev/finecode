from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.resource_uri import ResourceUri


@dataclasses.dataclass
class SemanticTokensDeltaPayload(code_action.RunActionPayload):
    uri: ResourceUri
    """The document to compute a delta for."""
    previous_result_id: str
    """The result_id from the previous full or delta response for this document."""


@dataclasses.dataclass
class SemanticTokensEdit:
    """A single edit to the previous delta-encoded integer array."""

    start: int
    """Zero-based offset into the previous data array (counted in integers)."""
    delete_count: int
    """Number of integers to delete starting at start."""
    data: list[int] = dataclasses.field(default_factory=list)
    """Replacement integers. Empty means deletion only."""


@dataclasses.dataclass
class SemanticTokensDeltaResult(code_action.RunActionResult):
    """Incremental update to a previous semantic tokens response.

    Edits operate on the wire-format integer array from the previous full
    response identified by result_id. If no handler recognises previous_result_id
    (e.g. after a server restart), it should return empty edits and leave
    result_id as None — the LSP endpoint will fall back to a full re-request.

    In most deployments there is exactly one active delta handler per language
    subaction.
    """

    edits: list[SemanticTokensEdit] = dataclasses.field(default_factory=list)
    """Ordered edits to apply to the previous data array."""
    result_id: str | None = None
    """New result identifier for the next delta request. None means the handler
    could not compute a delta; the endpoint will issue a full re-request."""

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, SemanticTokensDeltaResult):
            return
        self.edits.extend(other.edits)
        if other.result_id is not None:
            self.result_id = other.result_id


class TextDocumentSemanticTokensDeltaAction(code_action.Action):
    """Compute an incremental semantic token update since a previous response.

    previous_result_id identifies the baseline snapshot. The handler that owns
    that snapshot diffs it against a fresh tokenization and returns the edits
    as offsets into the previous wire-format integer array.

    Handlers run sequentially. If no handler recognises previous_result_id,
    it should return empty edits and leave result_id as None — the endpoint
    falls back to a full TextDocumentSemanticTokensAction request.
    """

    DESCRIPTION = "Compute an incremental semantic token update since a previous response."
    PAYLOAD_TYPE = SemanticTokensDeltaPayload
    RESULT_TYPE = SemanticTokensDeltaResult
    HANDLER_EXECUTION = code_action.HandlerExecution.SEQUENTIAL
