import contextlib
import dataclasses
import pathlib
import typing
from typing import Protocol

from finecode_extension_api import common_types
from finecode_extension_api.interfaces import ifilemanager

# reexport
Position = common_types.Position
Range = common_types.Range
FileNotFound = ifilemanager.FileNotFound
FileAlreadyExists = ifilemanager.FileAlreadyExists


@dataclasses.dataclass
class FileInfo:
    content: str
    version: str


@dataclasses.dataclass
class FileChangePartial:
    """The range of the document that changed."""

    range: Range
    """The new text for the provided range."""
    text: str


@dataclasses.dataclass
class FileChangeFull:
    # new file content
    text: str


FileChange = FileChangePartial | FileChangeFull


@dataclasses.dataclass
class FileOperationAuthor:
    id: str


@dataclasses.dataclass
class FileChangeEvent:
    file_path: pathlib.Path
    author: FileOperationAuthor
    change: FileChange


@dataclasses.dataclass
class FileOpenEvent:
    file_path: pathlib.Path


@dataclasses.dataclass
class FileCloseEvent:
    file_path: pathlib.Path
    author: FileOperationAuthor


@dataclasses.dataclass
class FileCreateEvent:
    file_path: pathlib.Path
    author: FileOperationAuthor


@dataclasses.dataclass
class FileDeleteEvent:
    file_path: pathlib.Path
    author: FileOperationAuthor


@dataclasses.dataclass
class FileRenameEvent:
    old_path: pathlib.Path
    new_path: pathlib.Path
    author: FileOperationAuthor


FileEvent = (
    FileOpenEvent
    | FileCloseEvent
    | FileChangeEvent
    | FileCreateEvent
    | FileDeleteEvent
    | FileRenameEvent
)


class FileAlreadyOpenError(Exception):
    """Raised when trying to open a file that's already open in the session."""

    def __init__(self, message: str) -> None:
        self.message = message


class FileVersionConflict(Exception):
    """Raised when a version-checked write finds the file changed underneath it.

    The write is refused, not retried: the content offered was derived from a
    version that is no longer current, so applying it would discard whatever
    change produced the newer version.
    """

    def __init__(
        self,
        file_path: pathlib.Path,
        expected_version: str,
        actual_version: str,
    ) -> None:
        self.file_path = file_path
        self.expected_version = expected_version
        self.actual_version = actual_version
        self.message = (
            f"{file_path} changed since it was read"
            f" (expected version {expected_version}, found {actual_version})"
        )
        super().__init__(self.message)


class IFileEditorSession(Protocol):
    """Read/write access to file content, for actions and handlers.

    Reasons for using sessions:
    - all operations should be authored to provide tracebility
    - some operations are author-specific, e.g. subscribe to changes of all opened by
      author files

    Reading and modifying are separate operations (ADR-0071):

    - `read_file` is shared and never waits. It always yields committed content,
      so a read nested anywhere inside another operation cannot deadlock.
    - `modify_file` claims the right to change a file, excluding other modifiers
      of the same path until the claim is released.

    A session is an authorship scope, not an operation scope: one session may
    span many independent concurrent operations (e.g. the per-file tasks of a
    batch format all share the session that started them). Exclusion is
    therefore keyed by file path, never by session.
    """

    def subscribe_to_all_events(
        self,
    ) -> contextlib.AbstractAsyncContextManager[FileEvent]:
        # TODO: bunch of change events at once?
        ...

    def read_file(
        self, file_path: pathlib.Path
    ) -> contextlib.AbstractAsyncContextManager[FileInfo]:
        """Read committed content. Shared; never waits on another operation.

        Raises:
            FileNotFound: `file_path` does not exist.
        """
        ...

    def modify_file(
        self, file_path: pathlib.Path
    ) -> contextlib.AbstractAsyncContextManager[FileInfo]:
        """Claim the right to modify `file_path` and yield its current content.

        Excludes other modifiers of the same path for as long as the claim is
        held; readers are unaffected. Claims are reentrant within one task, so
        claiming a path already claimed by the same call stack yields the
        already-claimed content instead of deadlocking; a nested operation that
        needs the content may therefore claim again.

        Raises `FileNotFound` when there is no file at `file_path` (use
        `claim_file` when the path may not exist yet).

        Holding a claim does not itself write anything. Commit the result with
        `save_file`, passing `if_version` to reject a write whose basis went
        stale.
        """
        ...

    def claim_file(
        self, file_path: pathlib.Path
    ) -> contextlib.AbstractAsyncContextManager[FileInfo | None]:
        """Claim the right to write `file_path`, which need not exist yet.

        Yields None when there is no file there. Excludes other claimants of the
        same path exactly as `modify_file` does -- claiming a path you are about
        to create is how two concurrent creators are serialized, and it has to
        happen before the existence check or the check races the claim.

        Use `modify_file` unless you may be creating the file: it is the same
        claim with the absence ruled out, so the type does not force every
        caller to handle a case that cannot arise.
        """
        ...

    async def file_exists(self, file_path: pathlib.Path) -> bool:
        """Whether there is a file at `file_path`, counting files this session
        has open but not yet saved.

        Advisory. The answer may be stale the instant it returns, so it must
        never guard a write -- a write guards itself, with `if_version` or with
        `overwrite` / `missing_ok`. It is for a dry run, whose every outcome is
        already a prediction (ADR-0085 rule 5).
        """
        ...

    async def create_file(
        self,
        file_path: pathlib.Path,
        file_content: str = "",
        *,
        overwrite: bool = False,
    ) -> None:
        """Create `file_path` with `file_content`.

        `overwrite=False` raises `FileAlreadyExists` if something is already
        there. Claims `file_path` for the duration; the claim is reentrant within
        one task, so this is callable from inside a claim you already hold.

        There is no `if_version`: a file that does not exist yet has no version
        to agree with, and `overwrite` is the precondition that applies (DA-7).
        """
        ...

    async def delete_file(
        self,
        file_path: pathlib.Path,
        *,
        if_version: str | None = None,
        missing_ok: bool = False,
        recursive: bool = False,
    ) -> None:
        """Delete `file_path`. Claims it for the duration (see `create_file`).

        `if_version` guards it exactly as on `save_file`. Raises `FileNotFound`
        if it is already gone, unless `missing_ok`.

        `recursive=True` deletes a directory tree. A directory has no version,
        so `if_version` must be `None` when `recursive` is set -- passing both
        is a contract error and raises `ValueError`.
        """
        ...

    async def rename_file(
        self,
        old_path: pathlib.Path,
        new_path: pathlib.Path,
        *,
        overwrite: bool = False,
        if_version: str | None = None,
    ) -> None:
        """Move `old_path` to `new_path`. Claims **both** paths, in sorted path
        order so that two concurrent renames of the same pair cannot deadlock.

        `if_version` guards `old_path`; `overwrite=False` raises
        `FileAlreadyExists` for an occupied `new_path`. `new_path` has no version
        to guard (DA-7).
        """
        ...

    async def read_file_version(self, file_path: pathlib.Path) -> str:
        """Read the file's current version without reading its content.

        Raises:
            FileNotFound: `file_path` does not exist.
        """
        ...

    async def save_file(
        self,
        file_path: pathlib.Path,
        file_content: str,
        if_version: str | None = None,
    ) -> None:
        """Publish `file_content` as the file's new content, in one commit.

        `if_version` makes the write conditional: if the file's current version
        differs, `FileVersionConflict` is raised and nothing is written.
        """
        ...

    # TODO
    # async def reread_file()


class IFileEditorProviderSession(IFileEditorSession, Protocol):
    """Everything in :class:`IFileEditorSession`, plus the file lifecycle
    operations used to mirror an external editor's state (open/close/change) —
    for the ER-server bridge that translates wire notifications into tracked
    file state. Not for use by actions or handlers.
    """

    async def open_file(self, file_path: pathlib.Path, content: str) -> None:
        # `content` seeds the tracked content directly — the caller (the
        # wire-protocol bridge) always already has it from the notification
        # that triggered the open, so there's never a need to read the file
        # from disk here.
        ...

    async def close_file(self, file_path: pathlib.Path) -> None: ...

    async def change_file(
        self, file_path: pathlib.Path, change: FileChange
    ) -> None: ...

    async def save_opened_file(self, file_path: pathlib.Path) -> None: ...

    async def subscribe_to_changes_of_opened_files(
        self,
    ) -> contextlib.AbstractAsyncContextManager[FileChangeEvent]:
        # TODO: bunch of change events at once?
        ...


class IFileEditor(Protocol):
    """Service for managing read/write access to the files, e.g:
    - read only for reading (other can read as well) (e.g. linter, IDE)
    - read for modyfing, excluding other modifiers of the same file until the
      modification is committed (e.g. code formatter)

    Readers are never made to wait for a modifier. A modification is published
    in a single commit, so the content a reader sees is always a consistent
    snapshot — there is no mid-edit state to protect readers from. What needs
    protecting is the *write*: two modifiers of one file exclude each other, and
    a write may be made conditional on the version it was based on
    (`save_file(..., if_version=...)`) so a lost update is refused rather than
    silently applied. See ADR-0071.

    IDE needs possibility to subscribe on changes to sync.
    IDE:
    - user opens a file in IDE   -> IDE sends 'open_file' and subscribes to changes, did by other
    - user edits the file in IDE -> IDE sends 'file_changed' with changes to FineCode. All subscribers get the changes
        -> file change should have an author
    - user saves the file in IDE -> IDE sends 'file_modified_on_disk' || TODO: distinguish saved file and not saved? or just keep opened?
    - user closes the file in IDE -> IDE sends 'close_file' and unsubscribes from changes

    External tools like language servers need possibility to subscribe not only to changes but also to open and close events.

    All tools access files via `ifileeditor.IFileEditor`, which stores the current(also not saved) content of the file.

    Reading/writing files: use always `ifileeditor.IFileEditor` to read and write files. It will check whether file is opened
    and opened content should be modified or file is not opened and it can be modified directly on disk.

    'opened files' ... files user sees and works with, not files which tools read.
    """

    def session(
        self, author: FileOperationAuthor
    ) -> typing.AsyncContextManager[IFileEditorProviderSession]:
        """Create a session for a specific author."""
        ...

    def get_opened_files(self) -> list[pathlib.Path]:
        # opened files from all sessions
        ...
