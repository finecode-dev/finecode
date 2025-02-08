import hashlib
from pathlib import Path

from finecode.extension_runner.interfaces import ifilemanager


class FileManager(ifilemanager.IFileManager):
    def __init__(self):
        self.file_content_by_path: dict[Path, str] = {}
        self.file_version_by_path: dict[Path, str] = {}
    
    def get_content(self, file_path: Path) -> str:
        if file_path in self.file_content_by_path:
            return self.file_content_by_path[file_path]
        
        # TODO: handle errors: file doesn't exist, cannot be opened etc
        with open(file_path, 'r') as f:
            file_content = f.read()
        self.file_content_by_path[file_path] = file_content

        return file_content

    def get_file_version(self, file_path: Path) -> str:
        # TODO: handle errors: file doesn't exist, cannot be opened etc
        with open(file_path, 'rb') as f:
            file_version = hashlib.file_digest(f, "sha256").hexdigest()
        self.file_version_by_path[file_path] = file_version
        return file_version
