import hashlib
from pathlib import Path
from typing import Callable

from finecode import pygls_types_utils
from finecode.extension_runner import domain
from finecode_extension_api.interfaces import ifilemanager, ilogger


class FileManager(ifilemanager.IFileManager):
    def __init__(
        self,
        docs_owned_by_client: list[str],
        get_document_func: Callable,
        save_document_func: Callable,
        logger: ilogger.ILogger,
    ) -> None:
        self.docs_owned_by_client = docs_owned_by_client
        self.get_document_func = get_document_func
        self.save_document_func = save_document_func
        self.logger = logger

    async def get_content(self, file_path: Path) -> str:
        file_uri = f"file://{file_path.as_posix()}"
        file_content: str = ""

        if file_uri in self.docs_owned_by_client:
            # docs owned by client cannot be cached, always read from client
            try:
                document_info = await self.get_document_func(file_uri)
                file_content = document_info.text
            except domain.TextDocumentNotOpened:
                file_content = self.read_content_file_from_fs(file_path=file_path)
        else:
            file_content = self.read_content_file_from_fs(file_path=file_path)

        return file_content

    async def get_file_version(self, file_path: Path) -> str:
        file_uri = pygls_types_utils.path_to_uri_str(file_path)
        file_version: str = ""

        if file_uri in self.docs_owned_by_client:
            # read file from client
            try:
                document_info = await self.get_document_func(file_uri)
                file_version = str(document_info.version)
            except domain.TextDocumentNotOpened:
                file_version = self.get_hash_of_file_from_fs(file_path=file_path)
        else:
            # TODO
            # st = file_path.stat()
            # file_version = f'{st.st_size},{st.st_mtime}'
            # if st.st_size != old.st_size:
            #     return True
            # if st.st_mtime != old.st_mtime:
            #     new_hash = Cache.hash_digest(res_src)
            #     if new_hash != old.hash:
            #         return True
            # return False

            file_version = self.get_hash_of_file_from_fs(file_path=file_path)

        # 12 chars is enough to distinguish. The whole value is 64 chars length and
        # is not really needed in logs
        file_version_readable = f"{file_version[:12]}..."
        self.logger.debug(f"Version of {file_path}: {file_version_readable}")
        return file_version

    async def save_file(self, file_path: Path, file_content: str) -> None:
        file_uri = pygls_types_utils.path_to_uri_str(file_path)
        if file_uri in self.docs_owned_by_client:
            await self.save_document_func(file_uri, file_content)
        else:
            with open(file_path, "w") as f:
                f.write(file_content)

    # helper methods
    def read_content_file_from_fs(self, file_path: Path) -> str:
        # don't use this method directly, use `get_content` instead
        # TODO: handle errors: file doesn't exist, cannot be opened etc
        self.logger.debug(f"Read file: {file_path}")
        with open(file_path, "r") as f:
            file_content = f.read()

        return file_content

    def get_hash_of_file_from_fs(self, file_path: Path) -> str:
        # don't use this method directly, use `get_file_version` instead
        # TODO: handle errors: file doesn't exist, cannot be opened etc
        with open(file_path, "rb") as f:
            file_version = hashlib.file_digest(f, "sha256").hexdigest()

        return file_version
