"""Neutral shared types for the MCP server.

Ownership: neutral. This module imports neither core, front, assembly nor CLI.
Stable tool data and schema field names may remain shared here.
"""

from __future__ import annotations

import dataclasses
import enum
import typing
from collections.abc import Awaitable, Callable
from typing import NotRequired, TypedDict

from finecode_extension_runner.schema_utils import (
    FieldSchema,
    JsonValue,
    PayloadSchema,
    SchemaType,
)

__all__ = [
    "ActionsPayload",
    "BuiltinToolArguments",
    "DescribedPayloadSchema",
    "JsonObject",
    "ProgressValue",
    "ProjectsPayload",
    "Question",
    "QuestionOutcome",
    "QuestionResult",
    "RawConfigPayload",
    "RestartPayload",
    "RunnersPayload",
    "ToolData",
    "ToolDependencies",
    "ToolDescriptor",
    "ToolEffects",
    "ToolFailure",
    "ToolInputSchema",
    "ToolOutcome",
    "ToolValue",
    "WmConnectionConfig",
    "WmQuestionParams",
    "WmQuestionReply",
]

JsonObject: typing.TypeAlias = dict[str, JsonValue]


class ToolInputSchema(TypedDict):
    type: SchemaType
    properties: dict[str, FieldSchema]
    required: NotRequired[list[str]]


class ToolDescriptor(TypedDict):
    name: str
    description: str
    inputSchema: ToolInputSchema


class DescribedPayloadSchema(PayloadSchema, total=False):
    description: str


class ProgressValue(TypedDict, total=False):
    type: str
    message: str
    title: str
    percentage: float | None


class WmQuestionParams(TypedDict, total=False):
    message: JsonValue
    options: list[JsonValue]
    timeoutSec: float | None


class QuestionOutcome(enum.StrEnum):
    ANSWERED = "answered"
    DECLINED = "declined"
    UNAVAILABLE = "unavailable"


@dataclasses.dataclass(frozen=True)
class WmConnectionConfig:
    client_id: str
    can_answer_choices: bool


@dataclasses.dataclass(frozen=True)
class Question:
    message: str
    options: tuple[str, ...]
    timeout_seconds: float | None


@dataclasses.dataclass(frozen=True)
class QuestionResult:
    outcome: QuestionOutcome
    value: str | None


class WmQuestionReply(TypedDict):
    outcome: QuestionOutcome
    value: NotRequired[str]


class ProjectsPayload(TypedDict):
    projects: list[JsonObject]


class RunnersPayload(TypedDict):
    runners: JsonValue


class ActionsPayload(TypedDict):
    actions: list[JsonObject]


class RawConfigPayload(TypedDict):
    rawConfig: JsonObject


class RestartPayload(TypedDict):
    restarted: bool
    previousPid: int | None
    port: int
    otherClientsDisconnected: list[str]


ToolValue: typing.TypeAlias = typing.Union[  # noqa: UP007; JsonValue is a string forward-ref, so `|` fails at runtime
    JsonValue,
    ProjectsPayload,
    RunnersPayload,
    ActionsPayload,
    RawConfigPayload,
    RestartPayload,
]


@dataclasses.dataclass(frozen=True)
class ToolData:
    value: ToolValue


@dataclasses.dataclass(frozen=True)
class ToolFailure:
    message: str


ToolOutcome: typing.TypeAlias = ToolData | ToolFailure


@dataclasses.dataclass(frozen=True)
class ToolDependencies:
    connection_config: Callable[[], WmConnectionConfig]
    current_client_label: Callable[[], str]


@dataclasses.dataclass(frozen=True)
class ToolEffects:
    partial_result: Callable[[JsonValue], Awaitable[None]]
    progress: Callable[[ProgressValue], Awaitable[None]]
    question: Callable[[Question], Awaitable[QuestionResult]]
    catalog_changed: Callable[[], Awaitable[None]]


class BuiltinToolArguments(TypedDict, total=False):
    project: str
    includeProcesses: bool
    action: str
    allProjects: bool
    env: str
    killInFlightRuns: bool
    rescan: bool
    format: bool
