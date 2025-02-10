import hashlib
from pathlib import Path
from typing import Callable

from finecode.extension_runner.interfaces import ifilemanager, ilogger
from finecode import lsp_types


class FileManager(ifilemanager.IFileManager):
    def __init__(self, docs_owned_by_client: list[str], get_document_func: Callable, logger: ilogger.ILogger) -> None:
        self.docs_owned_by_client = docs_owned_by_client
        self.get_document_func = get_document_func
        self.logger = logger

    def get_content(self, file_path: Path) -> str:
        file_uri = f"file://{file_path.as_posix()}"
        file_content: str = ""

        if file_uri in self.docs_owned_by_client:
            # docs owned by client cannot be cached, always read from client
            # TODO: make async?
            document_info = self.get_document_func(file_uri)
            assert isinstance(document_info, lsp_types.TextDocumentItem)
            return document_info.text
        else:
            # TODO: handle errors: file doesn't exist, cannot be opened etc
            with open(file_path, "r") as f:
                file_content = f.read()
            self.logger.debug(f"Read file: {file_path}")

            return file_content

    def get_file_version(self, file_path: Path) -> str:
        file_uri = f"file://{file_path.as_posix()}"
        file_version: str = ""

        if file_uri in self.docs_owned_by_client:
            # read file from client
            document_info = self.get_document_func(file_uri)
            assert isinstance(document_info, lsp_types.TextDocumentItem)
            file_version = str(document_info.version)
        else:
            # TODO: handle errors: file doesn't exist, cannot be opened etc
            with open(file_path, "rb") as f:
                file_version = hashlib.file_digest(f, "sha256").hexdigest()

        # 12 chars is enough to distinguish. The whole value is 64 chars length and is not really
        # needed in logs
        self.logger.debug(f"Version of {file_path}: {file_version[:12]}...")
        return file_version
