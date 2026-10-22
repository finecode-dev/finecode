"""Public observation records for agent runs."""

from __future__ import annotations

import dataclasses
import enum

from finecode_extension_api.resource_uri import ResourceUri

__all__ = [
    "AgentBackend",
    "AgentRunObservation",
    "HistoryDiscoveryStatus",
    "NativeHistoryFile",
    "NativeHistoryFormat",
    "NativeHistoryRole",
    "RelatedHistoryStatus",
    "UsageProvenance",
    "UsageScope",
    "UsageSource",
]


class AgentBackend(enum.StrEnum):
    PI = "pi"
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"


class NativeHistoryRole(enum.StrEnum):
    MAIN = "main"
    RELATED = "related"


class NativeHistoryFormat(enum.StrEnum):
    PI = "pi-session-jsonl"
    CLAUDE_CODE = "claude-session-jsonl"
    CODEX = "codex-rollout-jsonl"


class HistoryDiscoveryStatus(enum.StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class RelatedHistoryStatus(enum.StrEnum):
    SCANNED = "scanned"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class UsageScope(enum.StrEnum):
    SESSION = "session"
    MESSAGE_SUM = "message_sum"
    LAST_REQUEST = "last_request"
    UNKNOWN = "unknown"


class UsageSource(enum.StrEnum):
    PI_STATS = "pi_session_stats"
    PI_MESSAGES = "pi_message_usage"
    CLAUDE_MODELS = "claude_modelUsage"
    CLAUDE_RESULT = "claude_result_usage"
    CLAUDE_COST = "claude_total_cost_usd"
    CODEX_TURN = "codex_turn_usage"
    UNKNOWN = "unknown"


@dataclasses.dataclass(frozen=True)
class UsageProvenance:
    token_source: UsageSource
    token_scope: UsageScope
    cost_source: UsageSource
    cost_scope: UsageScope


@dataclasses.dataclass(frozen=True)
class NativeHistoryFile:
    role: NativeHistoryRole
    source_uri: ResourceUri
    native_relative_path: str
    native_session_id: str | None
    format: NativeHistoryFormat


@dataclasses.dataclass(frozen=True)
class AgentRunObservation:
    backend: AgentBackend
    backend_version: str | None
    session_id: str | None
    history_root: ResourceUri | None
    history_status: HistoryDiscoveryStatus
    files: list[NativeHistoryFile]
    related_status: RelatedHistoryStatus
    usage_provenance: UsageProvenance
    gaps: list[str]
