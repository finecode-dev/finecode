"""Legacy MCP protocol front.

Ownership: front. Owns negotiation and client state, the telemetry boundary,
response encoding and MCP outbound effects. Reaches the core only through
explicit service arguments; the core reaches this front only through the neutral
callback fields built by :meth:`dependencies` and :meth:`effects`.
"""

from __future__ import annotations

import enum
import json
import typing
from typing import NotRequired, TypedDict

from finecode_extension_runner.schema_utils import JsonValue
from loguru import logger

from finecode import telemetry
from finecode.mcp_server.contracts import (
    JsonObject,
    ProgressValue,
    Question,
    QuestionOutcome,
    QuestionResult,
    ToolDependencies,
    ToolDescriptor,
    ToolEffects,
    ToolFailure,
    WmConnectionConfig,
)
from finecode.mcp_server.core import ToolService
from finecode_jsonrpc import JsonRpcServerSession

__all__ = [
    "LegacyClientInfo",
    "LegacyElicitationResponse",
    "LegacyFront",
    "LegacyInitializeParams",
    "LegacyInitializeResult",
    "LegacyPingResult",
    "LegacyServerCapabilities",
    "LegacyServerInfo",
    "LegacyTextContent",
    "LegacyToolCall",
    "LegacyToolList",
    "LegacyToolResult",
    "LegacyToolsCapability",
]

_MCP_PROTOCOL_VERSION = "2025-06-18"
_SUPPORTED_MCP_PROTOCOL_VERSIONS = frozenset(
    {"2024-11-05", "2025-03-26", _MCP_PROTOCOL_VERSION}
)


class _TextContentType(enum.StrEnum):
    TEXT = "text"


class LegacyTextContent(TypedDict):
    type: _TextContentType
    text: str


class LegacyToolResult(TypedDict):
    content: list[LegacyTextContent]
    isError: NotRequired[bool]


class LegacyToolList(TypedDict):
    tools: list[ToolDescriptor]


class LegacyToolCall(TypedDict, total=False):
    name: str
    arguments: JsonObject | None


class LegacyClientInfo(TypedDict, total=False):
    name: str
    version: str


class LegacyInitializeParams(TypedDict, total=False):
    clientInfo: LegacyClientInfo | None
    capabilities: JsonObject | None
    protocolVersion: str


class LegacyToolsCapability(TypedDict):
    listChanged: bool


class LegacyServerCapabilities(TypedDict):
    tools: LegacyToolsCapability


class LegacyServerInfo(TypedDict):
    name: str
    version: str


class LegacyInitializeResult(TypedDict):
    protocolVersion: str
    capabilities: LegacyServerCapabilities
    serverInfo: LegacyServerInfo


class LegacyPingResult(TypedDict):
    pass


class LegacyElicitationResponse(TypedDict, total=False):
    action: str
    content: JsonObject | None


async def _noop(_params: JsonObject | None) -> None:
    pass


class LegacyFront:
    """The 2025-06-18 protocol surface: negotiation, encoding, outbound MCP."""

    def __init__(self, session: JsonRpcServerSession) -> None:
        self._session = session
        self._client_name: str | None = None
        self._client_can_elicit = False

    def connection_config(self) -> WmConnectionConfig:
        return WmConnectionConfig(
            client_id=f"mcp-{self._client_name}" if self._client_name else "mcp",
            can_answer_choices=self._client_can_elicit,
        )

    def current_client_label(self) -> str:
        return f"mcp-{self._client_name}" if self._client_name else "mcp"

    def dependencies(self) -> ToolDependencies:
        return ToolDependencies(
            connection_config=self.connection_config,
            current_client_label=self.current_client_label,
        )

    def effects(self) -> ToolEffects:
        return ToolEffects(
            partial_result=self.report_partial,
            progress=self.report_progress,
            question=self.ask_question,
            catalog_changed=self.catalog_changed,
        )

    def register(self, service: ToolService) -> None:
        session = self._session
        session.on_request("initialize", self.handle_initialize)
        session.on_request("ping", self.handle_ping)

        async def _list_tools(
            params: JsonObject | None,
        ) -> LegacyToolList:
            return await self.handle_list_tools(service, params)

        async def _call_tool(
            params: JsonObject | None,
        ) -> LegacyToolResult:
            return await self.handle_call_tool(service, params)

        session.on_request("tools/list", _list_tools)
        session.on_request("tools/call", _call_tool)
        session.on_notification("notifications/initialized", _noop)

    async def handle_initialize(
        self, params: JsonObject | None
    ) -> LegacyInitializeResult:
        requested = _MCP_PROTOCOL_VERSION
        if params:
            typed = typing.cast(LegacyInitializeParams, params)
            client_info = typed.get("clientInfo") or {}
            self._client_name = client_info.get("name")
            # Elicitation is a *client* capability: the client is the one that
            # owns a user to ask. Read here and nowhere else, because
            # `initialize` is the only place it is stated.
            self._client_can_elicit = bool(
                (typed.get("capabilities") or {}).get("elicitation")
            )
            asked_for = typed.get("protocolVersion")
            if asked_for in _SUPPORTED_MCP_PROTOCOL_VERSIONS:
                requested = asked_for
        if self._client_can_elicit and requested != _MCP_PROTOCOL_VERSION:
            # Nothing carries `elicitation/create` on an older revision, so a
            # client that declared the capability while pinning an older version
            # does not get asked.
            logger.info(
                f"MCP: client declared elicitation but speaks {requested}; not using it"
            )
            self._client_can_elicit = False
        return LegacyInitializeResult(
            protocolVersion=requested,
            capabilities={"tools": {"listChanged": True}},
            serverInfo={"name": "FineCode", "version": "1.0.0"},
        )

    async def handle_ping(self, _params: JsonObject | None) -> LegacyPingResult:
        return LegacyPingResult()

    async def handle_list_tools(
        self, service: ToolService, _params: JsonObject | None
    ) -> LegacyToolList:
        return LegacyToolList(tools=await service.list_tools())

    async def handle_call_tool(
        self, service: ToolService, params: JsonObject | None
    ) -> LegacyToolResult:
        """Dispatch one MCP tool call, projecting the core outcome to the wire."""
        if not params:
            return LegacyToolResult(
                content=[
                    LegacyTextContent(type=_TextContentType.TEXT, text="Missing params")
                ],
                isError=True,
            )
        typed = typing.cast(LegacyToolCall, params)
        name = typed.get("name", "")
        with telemetry.mcp_tool_span(name):
            arguments = dict(typed.get("arguments") or {})
            outcome = await service.call_tool(name, arguments)
            if isinstance(outcome, ToolFailure):
                return LegacyToolResult(
                    content=[
                        LegacyTextContent(
                            type=_TextContentType.TEXT, text=outcome.message
                        )
                    ],
                    isError=True,
                )
            return LegacyToolResult(
                content=[
                    LegacyTextContent(
                        type=_TextContentType.TEXT,
                        text=json.dumps(outcome.value),
                    )
                ]
            )

    async def report_partial(self, value: JsonValue) -> None:
        if self._session is None:
            return
        await self._session.send_notification(
            "notifications/message",
            {"level": "info", "data": value, "logger": "finecode"},
        )

    async def report_progress(self, value: ProgressValue) -> None:
        if self._session is None:
            return
        progress_type = value.get("type", "")
        message = value.get("message") or value.get("title") or ""
        percentage = value.get("percentage")
        log_data: dict[str, JsonValue] = {
            "progress_type": progress_type,
            "message": message,
        }
        if percentage is not None:
            log_data["percentage"] = percentage
        await self._session.send_notification(
            "notifications/message",
            {"level": "info", "data": log_data, "logger": "finecode.progress"},
        )

    async def ask_question(self, question: Question) -> QuestionResult:
        """Ask the AI client, reading live session and capability on each call."""
        if self._session is None or not self._client_can_elicit or not question.options:
            return QuestionResult(QuestionOutcome.UNAVAILABLE, None)
        try:
            answer = await self._session.send_request(
                "elicitation/create",
                {
                    "message": question.message,
                    # A flat object with primitive properties is all MCP's
                    # requested schema allows; an enum is how a closed set of
                    # answers is expressed within that.
                    "requestedSchema": {
                        "type": "object",
                        "properties": {
                            "choice": {
                                "type": "string",
                                "title": "Choice",
                                "enum": list(question.options),
                            }
                        },
                        "required": ["choice"],
                    },
                },
                timeout=question.timeout_seconds,
            )
        # The client may answer with an error, time out, or be gone; each is
        # the same thing to the run that asked.
        except Exception as exception:  # noqa: BLE001  # noqa: BLE001
            logger.warning(f"MCP: elicitation failed: {exception}")
            return QuestionResult(QuestionOutcome.UNAVAILABLE, None)
        if not isinstance(answer, dict):
            return QuestionResult(QuestionOutcome.UNAVAILABLE, None)
        response = typing.cast(LegacyElicitationResponse, answer)
        if response.get("action") != "accept":
            return QuestionResult(QuestionOutcome.DECLINED, None)
        choice = (response.get("content") or {}).get("choice")
        if choice not in question.options:
            logger.warning(f"MCP: client accepted with unoffered choice {choice!r}")
            return QuestionResult(QuestionOutcome.UNAVAILABLE, None)
        return QuestionResult(QuestionOutcome.ANSWERED, choice)

    async def catalog_changed(self) -> None:
        if self._session is None:
            return
        await self._session.send_notification("notifications/tools/list_changed", {})
