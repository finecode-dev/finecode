"""
Types for ER client.

LSP were reused where it was meaningful.
"""

from __future__ import annotations

import dataclasses
import collections.abc
import enum
import functools
import typing

EXIT = "exit"
INITIALIZE = "initialize"
INITIALIZED = "initialized"
SHUTDOWN = "shutdown"
CANCEL_REQUEST = "$/cancelRequest"
PROGRESS = "$/progress"
TEXT_DOCUMENT_DID_CLOSE = "textDocument/didClose"
TEXT_DOCUMENT_DID_OPEN = "textDocument/didOpen"
WORKSPACE_EXECUTE_COMMAND = "workspace/executeCommand"
WORKSPACE_APPLY_EDIT = "workspace/applyEdit"

DOCUMENT_GET = "documents/get"
PROJECT_RAW_CONFIG_GET = "projects/getRawConfig"


@dataclasses.dataclass
class BaseRequest:
    id: int | str
    """The request id."""
    method: str
    """The method name."""
    jsonrpc: str


@dataclasses.dataclass
class BaseResponse:
    id: int | str
    """The request id."""
    jsonrpc: str


@dataclasses.dataclass
class BaseNotification:
    method: str
    """The method name."""

    jsonrpc: str


@dataclasses.dataclass
class BaseResult: ...


@dataclasses.dataclass
class InitializeParams:
    capabilities: ClientCapabilities
    """The capabilities provided by the client (editor or tool)"""

    process_id: int | None = None
    """The process Id of the parent process that started
    the server.
    
    Is `null` if the process has not been started by another process.
    If the parent process is not alive then the server should exit."""

    client_info: ClientInfo | None = None
    """Information about the client
    
    @since 3.15.0"""
    # Since: 3.15.0

    locale: str | None = None
    """The locale the client is currently showing the user interface
    in. This must not necessarily be the locale of the operating
    system.
    
    Uses IETF language tags as the value's syntax
    (See https://en.wikipedia.org/wiki/IETF_language_tag)
    
    @since 3.16.0"""
    # Since: 3.16.0

    root_path: str | None = None
    """The rootPath of the workspace. Is null
    if no folder is open.
    
    @deprecated in favour of rootUri."""

    root_uri: str | None = None
    """The rootUri of the workspace. Is null if no
    folder is open. If both `rootPath` and `rootUri` are set
    `rootUri` wins.
    
    @deprecated in favour of workspaceFolders."""

    initialization_options: LSPAny | None = None
    """User provided initialization options."""

    trace: TraceValue | None = None
    """The initial trace setting. If omitted trace is disabled ('off')."""

    work_done_token: ProgressToken | None = None
    """An optional token that a server can use to report work done progress."""

    workspace_folders: collections.abc.Sequence[WorkspaceFolder] | None = None
    """The workspace folders configured in the client when the server starts.
    
    This property is only available if the client supports workspace folders.
    It can be `null` if the client supports workspace folders but none are
    configured.
    
    @since 3.6.0"""
    # Since: 3.6.0


@dataclasses.dataclass
class InitializeRequest(BaseRequest):
    params: InitializeParams
    method = "initialize"


@dataclasses.dataclass
class InitializeResult(BaseResult):
    """The result returned from an initialize request."""

    capabilities: ServerCapabilities
    """The capabilities the language server provides."""

    server_info: ServerInfo | None = None
    """Information about the server.
    
    @since 3.15.0"""
    # Since: 3.15.0


@dataclasses.dataclass
class InitializeResponse(BaseResponse):
    result: InitializeResult


@dataclasses.dataclass
class InitializeError:
    """The data type of the ResponseError if the
    initialize request fails."""

    retry: bool
    """Indicates whether the client execute the following retry logic:
    (1) show the message provided by the ResponseError to the user
    (2) user selects retry or cancel
    (3) if user selected retry the initialize method is sent again."""


@dataclasses.dataclass
class InitializedParams:
    pass


@dataclasses.dataclass
class ClientCapabilities:
    """Defines the capabilities provided by the client."""

    # workspace: WorkspaceClientCapabilities | None = None
    """Workspace specific client capabilities."""

    # text_document: TextDocumentClientCapabilities | None = None
    """Text document specific client capabilities."""

    # notebook_document: NotebookDocumentClientCapabilities | None = None
    """Capabilities specific to the notebook document support.
    
    @since 3.17.0"""
    # Since: 3.17.0

    # window: WindowClientCapabilities | None = None
    """Window specific client capabilities."""

    # general: GeneralClientCapabilities | None = None
    """General client capabilities.
    
    @since 3.16.0"""
    # Since: 3.16.0

    experimental: LSPAny | None = None
    """Experimental client capabilities."""


@dataclasses.dataclass
class ClientInfo:
    """Information about the client

    @since 3.15.0
    @since 3.18.0 ClientInfo type name added."""

    # Since:
    # 3.15.0
    # 3.18.0 ClientInfo type name added.

    name: str
    """The name of the client as defined by the client."""

    version: str | None = None
    """The client's version as defined by the client."""


LSPAny = typing.Any | None
"""The LSP any type.
Please note that strictly speaking a property with the value `undefined`
can't be converted into JSON preserving the property name. However for
convenience it is allowed and assumed that all these properties are
optional as well.
@since 3.17.0"""
# Since: 3.17.0


@enum.unique
class TraceValue(str, enum.Enum):
    Off = "off"
    """Turn tracing off."""
    Messages = "messages"
    """Trace messages only."""
    Verbose = "verbose"
    """Verbose message tracing."""


ProgressToken = int | str


@dataclasses.dataclass
class WorkspaceFolder:
    """A workspace folder inside a client."""

    uri: str
    """The associated URI for this workspace folder."""

    name: str
    """The name of the workspace folder. Used to refer to this
    workspace folder in the user interface."""


@enum.unique
class PositionEncodingKind(str, enum.Enum):
    """A set of predefined position encoding kinds.

    @since 3.17.0"""

    # Since: 3.17.0
    Utf8 = "utf-8"
    """Character offsets count UTF-8 code units (e.g. bytes)."""
    Utf16 = "utf-16"
    """Character offsets count UTF-16 code units.
    
    This is the default and must always be supported
    by servers"""
    Utf32 = "utf-32"
    """Character offsets count UTF-32 code units.
    
    Implementation note: these are the same as Unicode codepoints,
    so this `PositionEncodingKind` may also be used for an
    encoding-agnostic representation of character offsets."""


@dataclasses.dataclass
class SaveOptions:
    """Save options."""

    include_text: bool | None = None
    """The client is supposed to include the content on save."""


@dataclasses.dataclass
class TextDocumentSyncOptions:
    open_close: bool | None = None
    """Open and close notifications are sent to the server. If omitted open close notification should not
    be sent."""

    change: TextDocumentSyncKind | None = None
    """Change notifications are sent to the server. See TextDocumentSyncKind.None, TextDocumentSyncKind.Full
    and TextDocumentSyncKind.Incremental. If omitted it defaults to TextDocumentSyncKind.None."""

    will_save: bool | None = None
    """If present will save notifications are sent to the server. If omitted the notification should not be
    sent."""

    will_save_wait_until: bool | None = None
    """If present will save wait until requests are sent to the server. If omitted the request should not be
    sent."""

    save: bool | SaveOptions | None = None
    """If present save notifications are sent to the server. If omitted the notification should not be
    sent."""


@enum.unique
class TextDocumentSyncKind(int, enum.Enum):
    """Defines how the host (editor) should sync
    document changes to the language server."""

    None_ = 0
    """Documents should not be synced at all."""
    Full = 1
    """Documents are synced by always sending the full content
    of the document."""
    Incremental = 2
    """Documents are synced by sending the full content on open.
    After that only incremental updates to the document are
    send."""


@dataclasses.dataclass
class ExecuteCommandOptions:
    """The server capabilities of a {@link ExecuteCommandRequest}."""

    commands: collections.abc.Sequence[str]
    """The commands to be executed on the server"""

    work_done_progress: bool | None = None


@dataclasses.dataclass
class WorkspaceFoldersServerCapabilities:
    supported: bool | None = None
    """The server has support for workspace folders"""

    change_notifications: str | bool | None = None
    """Whether the server wants to receive workspace folder
    change notifications.
    
    If a string is provided the string is treated as an ID
    under which the notification is registered on the client
    side. The ID can be used to unregister for these events
    using the `client/unregisterCapability` request."""


@enum.unique
class FileOperationPatternKind(str, enum.Enum):
    """A pattern kind describing if a glob pattern matches a file a folder or
    both.

    @since 3.16.0"""

    # Since: 3.16.0
    File = "file"
    """The pattern matches a file only."""
    Folder = "folder"
    """The pattern matches a folder only."""


@dataclasses.dataclass
class FileOperationPatternOptions:
    """Matching options for the file operation pattern.

    @since 3.16.0"""

    # Since: 3.16.0

    ignore_case: bool | None = None
    """The pattern should be matched ignoring casing."""


@dataclasses.dataclass
class FileOperationPattern:
    """A pattern to describe in which file operation requests or notifications
    the server is interested in receiving.

    @since 3.16.0"""

    # Since: 3.16.0

    glob: str
    """The glob pattern to match. Glob patterns can have the following syntax:
    - `*` to match one or more characters in a path segment
    - `?` to match on one character in a path segment
    - `**` to match any number of path segments, including none
    - `{}` to group sub patterns into an OR expression. (e.g. `**/*.{ts,js}` matches all TypeScript and JavaScript files)
    - `[]` to declare a range of characters to match in a path segment (e.g., `example.[0-9]` to match on `example.0`, `example.1`, …)
    - `[!...]` to negate a range of characters to match in a path segment (e.g., `example.[!0-9]` to match on `example.a`, `example.b`, but not `example.0`)"""

    matches: FileOperationPatternKind | None = None
    """Whether to match files or folders with this pattern.
    
    Matches both if undefined."""

    options: FileOperationPatternOptions | None = None
    """Additional options used during matching."""


@dataclasses.dataclass
class FileOperationFilter:
    """A filter to describe in which file operation requests or notifications
    the server is interested in receiving.

    @since 3.16.0"""

    # Since: 3.16.0

    pattern: FileOperationPattern
    """The actual file operation pattern."""

    scheme: str | None = None
    """A Uri scheme like `file` or `untitled`."""


@dataclasses.dataclass
class FileOperationRegistrationOptions:
    """The options to register for file operations.

    @since 3.16.0"""

    # Since: 3.16.0

    filters: collections.abc.Sequence[FileOperationFilter]
    """The actual filters."""


@dataclasses.dataclass
class FileOperationOptions:
    """Options for notifications/requests for user operations on files.

    @since 3.16.0"""

    # Since: 3.16.0

    did_create: FileOperationRegistrationOptions | None = None
    """The server is interested in receiving didCreateFiles notifications."""

    will_create: FileOperationRegistrationOptions | None = None
    """The server is interested in receiving willCreateFiles requests."""

    did_rename: FileOperationRegistrationOptions | None = None
    """The server is interested in receiving didRenameFiles notifications."""

    will_rename: FileOperationRegistrationOptions | None = None
    """The server is interested in receiving willRenameFiles requests."""

    did_delete: FileOperationRegistrationOptions | None = None
    """The server is interested in receiving didDeleteFiles file notifications."""

    will_delete: FileOperationRegistrationOptions | None = None
    """The server is interested in receiving willDeleteFiles file requests."""


@dataclasses.dataclass
class TextDocumentContentOptions:
    """Text document content provider options.

    @since 3.18.0
    @proposed"""

    # Since: 3.18.0
    # Proposed

    schemes: collections.abc.Sequence[str]
    """The schemes for which the server provides content."""


@dataclasses.dataclass
class TextDocumentContentRegistrationOptions:
    """Text document content provider registration options.

    @since 3.18.0
    @proposed"""

    # Since: 3.18.0
    # Proposed

    schemes: collections.abc.Sequence[str]
    """The schemes for which the server provides content."""

    id: str | None = None
    """The id used to register the request. The id can be used to deregister
    the request again. See also Registration#id."""


@dataclasses.dataclass
class WorkspaceOptions:
    """Defines workspace specific capabilities of the server.

    @since 3.18.0"""

    # Since: 3.18.0

    workspace_folders: WorkspaceFoldersServerCapabilities | None = None
    """The server supports workspace folder.
    
    @since 3.6.0"""
    # Since: 3.6.0

    file_operations: FileOperationOptions | None = None
    """The server is interested in notifications/requests for operations on files.
    
    @since 3.16.0"""
    # Since: 3.16.0

    text_document_content: (
        TextDocumentContentOptions | TextDocumentContentRegistrationOptions | None
    ) = None
    """The server supports the `workspace/textDocumentContent` request.
    
    @since 3.18.0
    @proposed"""
    # Since: 3.18.0
    # Proposed


@dataclasses.dataclass
class ServerCapabilities:
    """Defines the capabilities provided by a language
    server."""

    position_encoding: PositionEncodingKind | str | None = None
    """The position encoding the server picked from the encodings offered
    by the client via the client capability `general.positionEncodings`.
    
    If the client didn't provide any position encodings the only valid
    value that a server can return is 'utf-16'.
    
    If omitted it defaults to 'utf-16'.
    
    @since 3.17.0"""
    # Since: 3.17.0

    text_document_sync: TextDocumentSyncOptions | TextDocumentSyncKind | None = None
    """Defines how text documents are synced. Is either a detailed structure
    defining each notification or for backwards compatibility the
    TextDocumentSyncKind number."""

    # notebook_document_sync: Optional[
    #     Union[NotebookDocumentSyncOptions, NotebookDocumentSyncRegistrationOptions]
    # ] = attrs.field(default=None)
    """Defines how notebook documents are synced.
    
    @since 3.17.0"""
    # Since: 3.17.0

    # completion_provider: Optional[CompletionOptions] = attrs.field(default=None)
    """The server provides completion support."""

    # hover_provider: Optional[Union[bool, HoverOptions]] = attrs.field(default=None)
    """The server provides hover support."""

    # signature_help_provider: Optional[SignatureHelpOptions] = attrs.field(default=None)
    """The server provides signature help support."""

    # declaration_provider: Optional[
    #     Union[bool, DeclarationOptions, DeclarationRegistrationOptions]
    # ] = attrs.field(default=None)
    """The server provides Goto Declaration support."""

    # definition_provider: Optional[Union[bool, DefinitionOptions]] = attrs.field(
    #     default=None
    # )
    """The server provides goto definition support."""

    # type_definition_provider: Optional[
    #     Union[bool, TypeDefinitionOptions, TypeDefinitionRegistrationOptions]
    # ] = attrs.field(default=None)
    """The server provides Goto Type Definition support."""

    # implementation_provider: Optional[
    #     Union[bool, ImplementationOptions, ImplementationRegistrationOptions]
    # ] = attrs.field(default=None)
    """The server provides Goto Implementation support."""

    # references_provider: Optional[Union[bool, ReferenceOptions]] = attrs.field(
    #     default=None
    # )
    """The server provides find references support."""

    # document_highlight_provider: Optional[Union[bool, DocumentHighlightOptions]] = (
    #     attrs.field(default=None)
    # )
    """The server provides document highlight support."""

    # document_symbol_provider: Optional[Union[bool, DocumentSymbolOptions]] = (
    #     attrs.field(default=None)
    # )
    """The server provides document symbol support."""

    # code_action_provider: Optional[Union[bool, CodeActionOptions]] = attrs.field(
    #     default=None
    # )
    """The server provides code actions. CodeActionOptions may only be
    specified if the client states that it supports
    `codeActionLiteralSupport` in its initial `initialize` request."""

    # code_lens_provider: Optional[CodeLensOptions] = attrs.field(default=None)
    """The server provides code lens."""

    # document_link_provider: Optional[DocumentLinkOptions] = attrs.field(default=None)
    """The server provides document link support."""

    # color_provider: Optional[
    #     Union[bool, DocumentColorOptions, DocumentColorRegistrationOptions]
    # ] = attrs.field(default=None)
    """The server provides color provider support."""

    # workspace_symbol_provider: Optional[Union[bool, WorkspaceSymbolOptions]] = (
    #     attrs.field(default=None)
    # )
    """The server provides workspace symbol support."""

    # document_formatting_provider: Optional[Union[bool, DocumentFormattingOptions]] = (
    #     attrs.field(default=None)
    # )
    """The server provides document formatting."""

    # document_range_formatting_provider: Optional[
    #     Union[bool, DocumentRangeFormattingOptions]
    # ] = attrs.field(default=None)
    """The server provides document range formatting."""

    # document_on_type_formatting_provider: Optional[DocumentOnTypeFormattingOptions] = (
    #     attrs.field(default=None)
    # )
    """The server provides document formatting on typing."""

    # rename_provider: Optional[Union[bool, RenameOptions]] = attrs.field(default=None)
    """The server provides rename support. RenameOptions may only be
    specified if the client states that it supports
    `prepareSupport` in its initial `initialize` request."""

    # folding_range_provider: Optional[
    #     Union[bool, FoldingRangeOptions, FoldingRangeRegistrationOptions]
    # ] = attrs.field(default=None)
    """The server provides folding provider support."""

    # selection_range_provider: Optional[
    #     Union[bool, SelectionRangeOptions, SelectionRangeRegistrationOptions]
    # ] = attrs.field(default=None)
    """The server provides selection range support."""

    execute_command_provider: ExecuteCommandOptions | None = None
    """The server provides execute command support."""

    # call_hierarchy_provider: Optional[
    #     Union[bool, CallHierarchyOptions, CallHierarchyRegistrationOptions]
    # ] = attrs.field(default=None)
    """The server provides call hierarchy support.
    
    @since 3.16.0"""
    # Since: 3.16.0

    # linked_editing_range_provider: Optional[
    #     Union[bool, LinkedEditingRangeOptions, LinkedEditingRangeRegistrationOptions]
    # ] = attrs.field(default=None)
    """The server provides linked editing range support.
    
    @since 3.16.0"""
    # Since: 3.16.0

    # semantic_tokens_provider: Optional[
    #     Union[SemanticTokensOptions, SemanticTokensRegistrationOptions]
    # ] = attrs.field(default=None)
    """The server provides semantic tokens support.
    
    @since 3.16.0"""
    # Since: 3.16.0

    # moniker_provider: Optional[
    #     Union[bool, MonikerOptions, MonikerRegistrationOptions]
    # ] = attrs.field(default=None)
    """The server provides moniker support.
    
    @since 3.16.0"""
    # Since: 3.16.0

    # type_hierarchy_provider: Optional[
    #     Union[bool, TypeHierarchyOptions, TypeHierarchyRegistrationOptions]
    # ] = attrs.field(default=None)
    """The server provides type hierarchy support.
    
    @since 3.17.0"""
    # Since: 3.17.0

    # inline_value_provider: Optional[
    #     Union[bool, InlineValueOptions, InlineValueRegistrationOptions]
    # ] = attrs.field(default=None)
    """The server provides inline values.
    
    @since 3.17.0"""
    # Since: 3.17.0

    # inlay_hint_provider: Optional[
    #     Union[bool, InlayHintOptions, InlayHintRegistrationOptions]
    # ] = attrs.field(default=None)
    """The server provides inlay hints.
    
    @since 3.17.0"""
    # Since: 3.17.0

    # diagnostic_provider: Optional[
    #     Union[DiagnosticOptions, DiagnosticRegistrationOptions]
    # ] = attrs.field(default=None)
    """The server has support for pull model diagnostics.
    
    @since 3.17.0"""
    # Since: 3.17.0

    # inline_completion_provider: Optional[Union[bool, InlineCompletionOptions]] = (
    #     attrs.field(default=None)
    # )
    """Inline completion options used during static registration.
    
    @since 3.18.0
    @proposed"""
    # Since: 3.18.0
    # Proposed

    workspace: WorkspaceOptions | None = None
    """Workspace specific server capabilities."""

    experimental: LSPAny | None = None
    """Experimental server capabilities."""


@dataclasses.dataclass
class ServerInfo:
    """Information about the server

    @since 3.15.0
    @since 3.18.0 ServerInfo type name added."""

    # Since:
    # 3.15.0
    # 3.18.0 ServerInfo type name added.

    name: str
    """The name of the server as defined by the server."""

    version: str | None = None
    """The server's version as defined by the server."""


@dataclasses.dataclass
class ExecuteCommandParams:
    """The parameters of a {@link ExecuteCommandRequest}."""

    command: str
    """The identifier of the actual command handler."""

    arguments: collections.abc.Sequence[LSPAny] | None = None
    """Arguments that the command should be invoked with."""

    work_done_token: ProgressToken | None = None
    """An optional token that a server can use to report work done progress."""


@dataclasses.dataclass
class ExecuteCommandRequest(BaseRequest):
    params: ExecuteCommandParams
    method = "workspace/executeCommand"


@dataclasses.dataclass
class ExecuteCommandResponse(BaseResponse):
    result: LSPAny | None = None


@dataclasses.dataclass
class DidOpenTextDocumentParams:
    """The parameters sent in an open text document notification"""

    text_document: TextDocumentItem
    """The document that was opened."""


@dataclasses.dataclass
class TextDocumentItem:
    """An item to transfer a text document from the client to the
    server."""

    uri: str
    """The text document's uri."""

    language_id: LanguageKind | str
    """The text document's language identifier."""

    version: int
    """The version number of this document (it will increase after each
    change, including undo/redo)."""

    text: str
    """The content of the opened text document."""


class LanguageKind(str, enum.Enum):
    """Predefined Language kinds
    @since 3.18.0"""

    # Since: 3.18.0
    Abap = "abap"
    WindowsBat = "bat"
    BibTeX = "bibtex"
    Clojure = "clojure"
    Coffeescript = "coffeescript"
    C = "c"
    Cpp = "cpp"
    CSharp = "csharp"
    Css = "css"
    D = "d"
    """@since 3.18.0
    @proposed"""
    # Since: 3.18.0
    # Proposed
    Delphi = "pascal"
    """@since 3.18.0
    @proposed"""
    # Since: 3.18.0
    # Proposed
    Diff = "diff"
    Dart = "dart"
    Dockerfile = "dockerfile"
    Elixir = "elixir"
    Erlang = "erlang"
    FSharp = "fsharp"
    GitCommit = "git-commit"
    GitRebase = "rebase"
    Go = "go"
    Groovy = "groovy"
    Handlebars = "handlebars"
    Haskell = "haskell"
    Html = "html"
    Ini = "ini"
    Java = "java"
    JavaScript = "javascript"
    JavaScriptReact = "javascriptreact"
    Json = "json"
    LaTeX = "latex"
    Less = "less"
    Lua = "lua"
    Makefile = "makefile"
    Markdown = "markdown"
    ObjectiveC = "objective-c"
    ObjectiveCpp = "objective-cpp"
    Pascal = "pascal"
    """@since 3.18.0
    @proposed"""
    # Since: 3.18.0
    # Proposed
    Perl = "perl"
    Perl6 = "perl6"
    Php = "php"
    Powershell = "powershell"
    Pug = "jade"
    Python = "python"
    R = "r"
    Razor = "razor"
    Ruby = "ruby"
    Rust = "rust"
    Scss = "scss"
    Sass = "sass"
    Scala = "scala"
    ShaderLab = "shaderlab"
    ShellScript = "shellscript"
    Sql = "sql"
    Swift = "swift"
    TypeScript = "typescript"
    TypeScriptReact = "typescriptreact"
    TeX = "tex"
    VisualBasic = "vb"
    Xml = "xml"
    Xsl = "xsl"
    Yaml = "yaml"


@dataclasses.dataclass
class DidCloseTextDocumentParams:
    """The parameters sent in a close text document notification"""

    text_document: TextDocumentIdentifier
    """The document that was closed."""


@dataclasses.dataclass
class TextDocumentIdentifier:
    """A literal to identify a text document in the client."""

    uri: str
    """The text document's uri."""


@dataclasses.dataclass
class ProgressParams:
    token: ProgressToken
    """The progress token provided by the client or server."""

    value: LSPAny
    """The progress data."""


@dataclasses.dataclass
class ProgressNotification(BaseNotification):
    params: ProgressParams
    method = "$/progress"


@dataclasses.dataclass
class ApplyWorkspaceEditParams:
    """The parameters passed via an apply workspace edit request."""

    edit: WorkspaceEdit
    """The edits to apply."""

    label: str | None = None
    """An optional label of the workspace edit. This label is
    presented in the user interface for example on an undo
    stack to undo the workspace edit."""

    metadata: WorkspaceEditMetadata | None = None
    """Additional data about the edit.
    
    @since 3.18.0
    @proposed"""
    # Since: 3.18.0
    # Proposed


@dataclasses.dataclass
class ApplyWorkspaceEditRequest(BaseRequest):
    params: ApplyWorkspaceEditParams
    method = "workspace/applyEdit"


@dataclasses.dataclass
class ApplyWorkspaceEditResult(BaseResult):
    """The result returned from the apply workspace edit request.

    @since 3.17 renamed from ApplyWorkspaceEditResponse"""

    # Since: 3.17 renamed from ApplyWorkspaceEditResponse

    applied: bool
    """Indicates whether the edit was applied or not."""

    failure_reason: str | None = None
    """An optional textual description for why the edit was not applied.
    This may be used by the server for diagnostic logging or to provide
    a suitable error for a request that triggered the edit."""

    failed_change: int | None = None
    """Depending on the client's failure handling strategy `failedChange` might
    contain the index of the change that failed. This property is only available
    if the client signals a `failureHandlingStrategy` in its client capabilities."""


@dataclasses.dataclass
class ApplyWorkspaceEditResponse(BaseResponse):
    result: ApplyWorkspaceEditResult


@dataclasses.dataclass
class WorkspaceEdit:
    """A workspace edit represents changes to many resources managed in the workspace. The edit
    should either provide `changes` or `documentChanges`. If documentChanges are present
    they are preferred over `changes` if the client can handle versioned document edits.

    Since version 3.13.0 a workspace edit can contain resource operations as well. If resource
    operations are present clients need to execute the operations in the order in which they
    are provided. So a workspace edit for example can consist of the following two changes:
    (1) a create file a.txt and (2) a text document edit which insert text into file a.txt.

    An invalid sequence (e.g. (1) delete file a.txt and (2) insert text into file a.txt) will
    cause failure of the operation. How the client recovers from the failure is described by
    the client capability: `workspace.workspaceEdit.failureHandling`"""

    changes: collections.abc.Mapping[str, collections.abc.Sequence[TextEdit]] | None = (
        None
    )
    """Holds changes to existing resources."""

    document_changes: (
        collections.abc.Sequence[
            TextDocumentEdit | CreateFile | RenameFile | DeleteFile
        ]
        | None
    ) = None
    """Depending on the client capability `workspace.workspaceEdit.resourceOperations` document changes
    are either an array of `TextDocumentEdit`s to express changes to n different text documents
    where each text document edit addresses a specific version of a text document. Or it can contain
    above `TextDocumentEdit`s mixed with create, rename and delete file / folder operations.
    
    Whether a client supports versioned document edits is expressed via
    `workspace.workspaceEdit.documentChanges` client capability.
    
    If a client neither supports `documentChanges` nor `workspace.workspaceEdit.resourceOperations` then
    only plain `TextEdit`s using the `changes` property are supported."""

    change_annotations: (
        collections.abc.Mapping[ChangeAnnotationIdentifier, ChangeAnnotation] | None
    ) = None
    """A map of change annotations that can be referenced in `AnnotatedTextEdit`s or create, rename and
    delete file / folder operations.
    
    Whether clients honor this property depends on the client capability `workspace.changeAnnotationSupport`.
    
    @since 3.16.0"""
    # Since: 3.16.0


@dataclasses.dataclass
class CreateFile:
    """Create file operation."""

    uri: str
    """The resource to create."""

    kind: typing.Literal["create"] = "create"
    """A create"""

    options: CreateFileOptions | None = None
    """Additional options"""

    annotation_id: ChangeAnnotationIdentifier | None = None
    """An optional annotation identifier describing the operation.
    
    @since 3.16.0"""
    # Since: 3.16.0


@dataclasses.dataclass
class CreateFileOptions:
    """Options to create a file."""

    overwrite: bool | None = None
    """Overwrite existing file. Overwrite wins over `ignoreIfExists`"""

    ignore_if_exists: bool | None = None
    """Ignore if exists."""


ChangeAnnotationIdentifier = str


@dataclasses.dataclass
class RenameFile:
    """Rename file operation"""

    old_uri: str
    """The old (existing) location."""

    new_uri: str
    """The new location."""

    kind: typing.Literal["rename"] = "rename"
    """A rename"""

    options: RenameFileOptions | None = None
    """Rename options."""

    annotation_id: ChangeAnnotationIdentifier | None = None
    """An optional annotation identifier describing the operation.
    
    @since 3.16.0"""
    # Since: 3.16.0


@dataclasses.dataclass
class RenameFileOptions:
    """Rename file options"""

    overwrite: bool | None = None
    """Overwrite target if existing. Overwrite wins over `ignoreIfExists`"""

    ignore_if_exists: bool | None = None
    """Ignores if target exists."""


@dataclasses.dataclass
class DeleteFile:
    """Delete file operation"""

    uri: str
    """The file to delete."""

    kind: typing.Literal["delete"] = "delete"
    """A delete"""

    options: DeleteFileOptions | None = None
    """Delete options."""

    annotation_id: ChangeAnnotationIdentifier | None = None
    """An optional annotation identifier describing the operation.
    
    @since 3.16.0"""
    # Since: 3.16.0


@dataclasses.dataclass
class DeleteFileOptions:
    """Delete file options"""

    recursive: bool | None = None
    """Delete the content recursively if a folder is denoted."""

    ignore_if_not_exists: bool | None = None
    """Ignore the operation if the file doesn't exist."""


@dataclasses.dataclass
class ChangeAnnotation:
    """Additional information that describes document changes.

    @since 3.16.0"""

    # Since: 3.16.0

    label: str
    """A human-readable string describing the actual change. The string
    is rendered prominent in the user interface."""

    needs_confirmation: bool | None = None
    """A flag which indicates that user confirmation is needed
    before applying the change."""

    description: str | None = None
    """A human-readable string which is rendered less prominent in
    the user interface."""


@dataclasses.dataclass
class TextEdit:
    """A text edit applicable to a text document."""

    range: Range
    """The range of the text document to be manipulated. To insert
    text into a document create a range where start === end."""

    new_text: str
    """The string to be inserted. For delete operations use an
    empty string."""


@dataclasses.dataclass
class Range:
    """A range in a text document expressed as (zero-based) start and end positions.

    If you want to specify a range that contains a line including the line ending
    character(s) then use an end position denoting the start of the next line.
    For example:
    ```ts
    {
        start: { line: 5, character: 23 }
        end : { line 6, character : 0 }
    }
    ```"""

    start: Position
    """The range's start position."""

    end: Position
    """The range's end position."""

    @typing.override
    def __eq__(self, o: object) -> bool:
        if not isinstance(o, Range):
            return NotImplemented
        return (self.start == o.start) and (self.end == o.end)

    @typing.override
    def __repr__(self) -> str:
        return f"{self.start!r}-{self.end!r}"


@dataclasses.dataclass
@functools.total_ordering
class Position:
    """Position in a text document expressed as zero-based line and character
    offset. Prior to 3.17 the offsets were always based on a UTF-16 string
    representation. So a string of the form `a𐐀b` the character offset of the
    character `a` is 0, the character offset of `𐐀` is 1 and the character
    offset of b is 3 since `𐐀` is represented using two code units in UTF-16.
    Since 3.17 clients and servers can agree on a different string encoding
    representation (e.g. UTF-8). The client announces it's supported encoding
    via the client capability [`general.positionEncodings`](https://microsoft.github.io/language-server-protocol/specifications/specification-current/#clientCapabilities).
    The value is an array of position encodings the client supports, with
    decreasing preference (e.g. the encoding at index `0` is the most preferred
    one). To stay backwards compatible the only mandatory encoding is UTF-16
    represented via the string `utf-16`. The server can pick one of the
    encodings offered by the client and signals that encoding back to the
    client via the initialize result's property
    [`capabilities.positionEncoding`](https://microsoft.github.io/language-server-protocol/specifications/specification-current/#serverCapabilities). If the string value
    `utf-16` is missing from the client's capability `general.positionEncodings`
    servers can safely assume that the client supports UTF-16. If the server
    omits the position encoding in its initialize result the encoding defaults
    to the string value `utf-16`. Implementation considerations: since the
    conversion from one encoding into another requires the content of the
    file / line the conversion is best done where the file is read which is
    usually on the server side.

    Positions are line end character agnostic. So you can not specify a position
    that denotes `\r|\n` or `\n|` where `|` represents the character offset.

    @since 3.17.0 - support for negotiated position encoding."""

    # Since: 3.17.0 - support for negotiated position encoding.

    line: int
    """Line position in a document (zero-based)."""

    character: int
    """Character offset on a line in a document (zero-based).
    
    The meaning of this offset is determined by the negotiated
    `PositionEncodingKind`."""

    @typing.override
    def __eq__(self, o: object) -> bool:
        if not isinstance(o, Position):
            return NotImplemented
        return (self.line, self.character) == (o.line, o.character)

    def __gt__(self, o: object) -> bool:
        if not isinstance(o, Position):
            return NotImplemented
        return (self.line, self.character) > (o.line, o.character)

    @typing.override
    def __repr__(self) -> str:
        return f"{self.line}:{self.character}"


@dataclasses.dataclass
class WorkspaceEditMetadata:
    """Additional data about a workspace edit.

    @since 3.18.0
    @proposed"""

    # Since: 3.18.0
    # Proposed

    is_refactoring: bool | None = None
    """Signal to the editor that this edit is a refactoring."""


@dataclasses.dataclass
class OptionalVersionedTextDocumentIdentifier:
    """A text document identifier to optionally denote a specific version of a text document."""

    uri: str
    """The text document's uri."""

    version: int | None = None
    """The version number of this document. If a versioned text document identifier
    is sent from the server to the client and the file is not open in the editor
    (the server has not received an open notification before) the server can send
    `null` to indicate that the version is unknown and the content on disk is the
    truth (as specified with document content ownership)."""


@dataclasses.dataclass
class TextDocumentEdit:
    """Describes textual changes on a text document. A TextDocumentEdit describes all changes
    on a document version Si and after they are applied move the document to version Si+1.
    So the creator of a TextDocumentEdit doesn't need to sort the array of edits or do any
    kind of ordering. However the edits must be non overlapping."""

    text_document: OptionalVersionedTextDocumentIdentifier
    """The text document to change."""

    edits: collections.abc.Sequence[TextEdit | AnnotatedTextEdit | SnippetTextEdit]
    """The edits to be applied.
    
    @since 3.16.0 - support for AnnotatedTextEdit. This is guarded using a
    client capability.
    
    @since 3.18.0 - support for SnippetTextEdit. This is guarded using a
    client capability."""
    # Since:
    # 3.16.0 - support for AnnotatedTextEdit. This is guarded using a client capability.
    # 3.18.0 - support for SnippetTextEdit. This is guarded using a client capability.


@dataclasses.dataclass
class AnnotatedTextEdit:
    """A special text edit with an additional change annotation.

    @since 3.16.0."""

    # Since: 3.16.0.

    annotation_id: ChangeAnnotationIdentifier
    """The actual identifier of the change annotation"""

    range: Range
    """The range of the text document to be manipulated. To insert
    text into a document create a range where start === end."""

    new_text: str
    """The string to be inserted. For delete operations use an
    empty string."""


@dataclasses.dataclass
class SnippetTextEdit:
    """An interactive text edit.

    @since 3.18.0
    @proposed"""

    # Since: 3.18.0
    # Proposed

    range: Range
    """The range of the text document to be manipulated."""

    snippet: StringValue
    """The snippet to be inserted."""

    annotation_id: ChangeAnnotationIdentifier | None = None
    """The actual identifier of the snippet edit."""


@dataclasses.dataclass
class StringValue:
    """A string value used as a snippet is a template which allows to insert text
    and to control the editor cursor when insertion happens.

    A snippet can define tab stops and placeholders with `$1`, `$2`
    and `${3:foo}`. `$0` defines the final tab stop, it defaults to
    the end of the snippet. Variables are defined with `$name` and
    `${name:default value}`.

    @since 3.18.0
    @proposed"""

    # Since: 3.18.0
    # Proposed

    value: str
    """The snippet string."""

    kind: typing.Literal["snippet"] = "snippet"
    """The kind of string value."""


@dataclasses.dataclass
class GetDocumentParams:
    uri: str


@dataclasses.dataclass
class GetDocumentRequest(BaseRequest):
    params: GetDocumentParams
    method = "documents/get"


@dataclasses.dataclass
class GetDocumentResult(BaseResult):
    uri: str
    version: str
    text: str


@dataclasses.dataclass
class GetDocumentResponse(BaseResponse):
    result: GetDocumentResult


@dataclasses.dataclass
class GetProjectRawConfigParams:
    project_def_path: str


@dataclasses.dataclass
class GetProjectRawConfigRequest(BaseRequest):
    params: GetProjectRawConfigParams
    method = "projects/getRawConfig"


@dataclasses.dataclass
class GetProjectRawConfigResult(BaseResult):
    # stringified json
    config: str


@dataclasses.dataclass
class GetProjectRawConfigResponse(BaseResponse):
    result: GetProjectRawConfigResult


@dataclasses.dataclass
class InitializedNotification(BaseNotification):
    """The initialized notification is sent from the client to the
    server after the client is fully initialized and the server
    is allowed to send requests from the server to the client."""

    params: InitializedParams

    method = "initialized"
    """The method to be invoked."""


@dataclasses.dataclass
class DidOpenTextDocumentNotification(BaseNotification):
    """The document open notification is sent from the client to the server to signal
    newly opened text documents. The document's truth is now managed by the client
    and the server must not try to read the document's truth using the document's
    uri. Open in this sense means it is managed by the client. It doesn't necessarily
    mean that its content is presented in an editor. An open notification must not
    be sent more than once without a corresponding close notification send before.
    This means open and close notification must be balanced and the max open count
    is one."""

    params: DidOpenTextDocumentParams
    method = "textDocument/didOpen"


@dataclasses.dataclass
class DidCloseTextDocumentNotification(BaseNotification):
    """The document close notification is sent from the client to the server when
    the document got closed in the client. The document's truth now exists where
    the document's uri points to (e.g. if the document's uri is a file uri the
    truth now exists on disk). As with the open notification the close notification
    is about managing the document's content. Receiving a close notification
    doesn't mean that the document was open in an editor before. A close
    notification requires a previous open notification to be sent."""

    params: DidOpenTextDocumentParams
    method = "textDocument/didClose"


@dataclasses.dataclass
class CancelParams:
    id: int | str
    """The request id to cancel."""


@dataclasses.dataclass
class CancelNotification(BaseNotification):
    params: CancelParams
    method = "$/cancelRequest"


@dataclasses.dataclass
class ShutdownRequest(BaseRequest):
    params: None = None


@dataclasses.dataclass
class ShutdownResponse(BaseResponse): ...


@dataclasses.dataclass
class ExitNotification(BaseNotification):
    params: None = None

    method = "exit"
    """The method to be invoked."""


METHOD_TO_TYPES: dict[
    str,
    tuple[type[BaseRequest], type | None, type[BaseResponse], type[BaseResult] | None]
    | tuple[type[BaseNotification], type | None, None, None],
] = {
    INITIALIZE: (
        InitializeRequest,
        InitializeParams,
        InitializeResponse,
        InitializeResult,
    ),
    INITIALIZED: (InitializedNotification, InitializedParams, None, None),
    CANCEL_REQUEST: (CancelNotification, CancelParams, None, None),
    SHUTDOWN: (ShutdownRequest, None, ShutdownResponse, None),
    PROGRESS: (ProgressNotification, ProgressParams, None, None),
    EXIT: (ExitNotification, None, None, None),
    WORKSPACE_EXECUTE_COMMAND: (
        ExecuteCommandRequest,
        ExecuteCommandParams,
        ExecuteCommandResponse,
        None,
    ),
    WORKSPACE_APPLY_EDIT: (
        ApplyWorkspaceEditRequest,
        ApplyWorkspaceEditParams,
        ApplyWorkspaceEditResponse,
        ApplyWorkspaceEditResult,
    ),
    DOCUMENT_GET: (
        GetDocumentRequest,
        GetDocumentParams,
        GetDocumentResponse,
        GetDocumentResult,
    ),
    PROJECT_RAW_CONFIG_GET: (GetProjectRawConfigRequest, GetProjectRawConfigParams, GetProjectRawConfigResponse, GetProjectRawConfigResult),
    TEXT_DOCUMENT_DID_OPEN: (
        DidOpenTextDocumentNotification,
        DidOpenTextDocumentParams,
        None,
        None,
    ),
    TEXT_DOCUMENT_DID_CLOSE: (
        DidCloseTextDocumentNotification,
        DidCloseTextDocumentParams,
        None,
        None,
    ),
}
