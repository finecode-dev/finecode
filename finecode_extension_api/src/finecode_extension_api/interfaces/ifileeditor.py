import typing
import dataclasses
import collections.abc
import contextlib
import pathlib
from typing import Protocol

from finecode_extension_api import common_types

# reexport
Position = common_types.Position
Range = common_types.Range


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


FileEvent = FileOpenEvent | FileCloseEvent | FileChangeEvent

class FileAlreadyOpenError(Exception):
    """Raised when trying to open a file that's already open in the session."""

    def __init__(self, message: str) -> None:
        self.message = message


class IFileEditorSession(Protocol):
    # Reasons for using sessions:
    # - all operations should be authored to provide tracebility
    # - some operations are author-specific, e.g. subscribe to changes of all opened by
    #   author files
    async def change_file(
        self, file_path: pathlib.Path, change: FileChange
    ) -> None: ...

    @contextlib.asynccontextmanager
    async def subscribe_to_changes_of_opened_files(
        self,
    ) -> collections.abc.AsyncIterator[FileChangeEvent]:
        # TODO: bunch of change events at once?
        ...

    async def open_file(self, file_path: pathlib.Path) -> None: ...

    async def save_opened_file(self, file_path: pathlib.Path) -> None: ...

    async def close_file(self, file_path: pathlib.Path) -> None: ...

    @contextlib.asynccontextmanager
    async def subscribe_to_all_events(
        self,
    ) -> collections.abc.AsyncIterator[FileEvent]:
        # TODO: bunch of change events at once?
        ...

    @contextlib.asynccontextmanager
    async def read_file(
        self, file_path: pathlib.Path, block: bool = False
    ) -> collections.abc.AsyncIterator[FileInfo]: ...

    async def read_file_version(self, file_path: pathlib.Path) -> str:
        # in case only file version is needed without content
        ...

    async def save_file(self, file_path: pathlib.Path, file_content: str) -> None: ...

    # TODO
    # async def reread_file()


class IFileEditor(Protocol):
    """Service for managing read/write access to the files, e.g:
    - read only for reading (other can read as well) (e.g. linter)
    - read for modyfing and block until modification is done (e.g. code formatter)
    - read for modyfing without blocking (e.g. by IDE)

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

    'opened files' ... files user sees and works with, not files which tools read
    """

    @contextlib.asynccontextmanager
    async def session(
        self, author: FileOperationAuthor
    ) -> typing.AsyncContextManager[IFileEditorSession]:
        """Create a session for a specific author."""
        ...

    def get_opened_files(self) -> list[pathlib.Path]:
        # opened files from all sessions
        ...
