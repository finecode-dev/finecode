"""Record types shared by the pyrefly host transport actions.

The identity travels on every announce and every forward: the host needs the
project's interpreter, extra paths, rendered config and pyrefly version before
it can answer anything about that project (ADR-0108). The query payloads are
flat ints rather than Position/Range because the workspace runner serializes
through ``dataclasses.asdict``; the host rebuilds the dict ``LspService``
expects.

No ``from __future__ import annotations`` here, deliberately: PEP 655's
``NotRequired`` is only reflected in a TypedDict's ``__required_keys__`` when the
annotation is evaluated at class creation, and the action payload converter
reads those keys. Postponed evaluation would make ``displayTypeErrors`` look
required and reject the announce that legitimately omits it.
"""

import dataclasses
import enum
import hashlib
import json
from typing import Any, Literal, NotRequired, TypedDict

from fine_inspect_code.diagnostic_types import Diagnostic
from finecode_extension_api import code_action
from finecode_extension_api.resource_uri import ResourceUri


class PyreflyTopology(enum.StrEnum):
    PER_PROJECT = "per-project"
    WORKSPACE = "workspace"


class PyreflyFolderSettings(TypedDict):
    """The client settings one workspace folder is answered with."""

    pythonPath: str
    extraPaths: list[str]
    configPath: str
    displayTypeErrors: NotRequired[Literal["force-on"]]


@dataclasses.dataclass(frozen=True)
class PyreflyProjectIdentity:
    """Carried on every announce and every forward.

    The fingerprint is sha256 of the canonical JSON of the identity fields,
    with object keys sorted and ``extraPaths`` sorted. A change is a push, not
    a restart (M3).
    """

    project_path: str
    version: str
    topology: PyreflyTopology
    settings: PyreflyFolderSettings
    fingerprint: str

    @classmethod
    def build(
        cls,
        *,
        project_path: str,
        version: str,
        topology: PyreflyTopology,
        settings: PyreflyFolderSettings,
    ) -> "PyreflyProjectIdentity":
        canonical = json.dumps(
            {
                "projectPath": project_path,
                "version": version,
                "topology": str(topology),
                "settings": {
                    "pythonPath": settings["pythonPath"],
                    "extraPaths": sorted(settings["extraPaths"]),
                    "configPath": settings["configPath"],
                    "displayTypeErrors": settings.get("displayTypeErrors"),
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return cls(
            project_path=project_path,
            version=version,
            topology=topology,
            settings=settings,
            fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )


@dataclasses.dataclass
class AnnouncePyreflyProjectPayload(code_action.RunActionPayload):
    identity: PyreflyProjectIdentity


@dataclasses.dataclass
class AnnouncePyreflyProjectResult(code_action.RunActionResult):
    registered: bool
    # registered False means this project was refused (version, topology, or
    # path). error is the configuration-error text; None when registered.
    error: str | None = None


@dataclasses.dataclass
class CheckFilesOnPyreflyHostPayload(code_action.RunActionPayload):
    identity: PyreflyProjectIdentity
    # At most ``PYREFLY_CHECK_CHUNK_SIZE`` files per forward.
    files: list[ResourceUri]
    # True on the first chunk of a run; the host coalesces sweeps.
    sweep: bool
    # uri -> text, for documents the caller holds. The host ignores a uri it
    # already has in _open_documents (buffer precedence).
    contents: dict[str, str]


PYREFLY_CHECK_CHUNK_SIZE = 50


class FileCheckStatus(enum.StrEnum):
    CHECKED = "checked"
    FAILED = "failed"


@dataclasses.dataclass
class FileCheckOutcome:
    uri: ResourceUri
    status: FileCheckStatus
    diagnostics: list[Diagnostic]  # empty when FAILED, or when CHECKED and clean
    error: str | None = None  # set iff status is FAILED


@dataclasses.dataclass
class CheckFilesOnPyreflyHostResult(code_action.RunActionResult):
    files: list[FileCheckOutcome]


class PyreflyQueryKind(enum.StrEnum):
    HOVER = "hover"
    DEFINITION = "definition"
    REFERENCES = "references"
    TYPE_DEFINITION = "type_definition"
    IMPLEMENTATION = "implementation"
    DOCUMENT_HIGHLIGHT = "document_highlight"
    SEMANTIC_TOKENS = "semantic_tokens"
    INLAY_HINTS = "inlay_hints"
    CALL_HIERARCHY_PREPARE = "call_hierarchy_prepare"
    CALL_HIERARCHY_INCOMING = "call_hierarchy_incoming"
    CALL_HIERARCHY_OUTGOING = "call_hierarchy_outgoing"
    TYPE_HIERARCHY_PREPARE = "type_hierarchy_prepare"
    TYPE_HIERARCHY_SUPERTYPES = "type_hierarchy_supertypes"
    TYPE_HIERARCHY_SUBTYPES = "type_hierarchy_subtypes"


PyreflyPositionQueryKind = Literal[
    PyreflyQueryKind.HOVER,
    PyreflyQueryKind.DEFINITION,
    PyreflyQueryKind.REFERENCES,
    PyreflyQueryKind.TYPE_DEFINITION,
    PyreflyQueryKind.IMPLEMENTATION,
    PyreflyQueryKind.DOCUMENT_HIGHLIGHT,
    PyreflyQueryKind.CALL_HIERARCHY_PREPARE,
    PyreflyQueryKind.TYPE_HIERARCHY_PREPARE,
]
PyreflyRangeQueryKind = Literal[
    PyreflyQueryKind.SEMANTIC_TOKENS, PyreflyQueryKind.INLAY_HINTS
]
PyreflyItemQueryKind = Literal[
    PyreflyQueryKind.CALL_HIERARCHY_INCOMING,
    PyreflyQueryKind.CALL_HIERARCHY_OUTGOING,
    PyreflyQueryKind.TYPE_HIERARCHY_SUPERTYPES,
    PyreflyQueryKind.TYPE_HIERARCHY_SUBTYPES,
]


@dataclasses.dataclass(frozen=True)
class PyreflyPositionQuery:
    kind: PyreflyPositionQueryKind
    file: ResourceUri
    content: str
    position_line: int
    position_character: int
    include_declaration: bool = True  # read only when kind is REFERENCES


@dataclasses.dataclass(frozen=True)
class PyreflyRangeQuery:
    kind: PyreflyRangeQueryKind
    file: ResourceUri
    content: str
    # None means the whole document (semantic tokens full). Inlay hints always
    # set all four.
    start_line: int | None
    start_character: int | None
    end_line: int | None
    end_character: int | None


@dataclasses.dataclass(frozen=True)
class PyreflyItemQuery:
    kind: PyreflyItemQueryKind
    # The item previously returned by prepare, serialized as the same JSON
    # object the handler already passes to LspService. FineCode items are uri +
    # range + selection range; not an lsprotocol type.
    item: dict[str, str | int | dict[str, int]]


@dataclasses.dataclass
class QueryPyreflyHostPayload(code_action.RunActionPayload):
    identity: PyreflyProjectIdentity
    query: PyreflyPositionQuery | PyreflyRangeQuery | PyreflyItemQuery


@dataclasses.dataclass
class QueryPyreflyHostResult(code_action.RunActionResult):
    # JSON result of the LSP request, or None when the server returned null
    # (a real empty hover). A failure raises ActionFailedException; it is not
    # encoded as raw = None.
    raw: dict[str, Any] | list[Any] | None
