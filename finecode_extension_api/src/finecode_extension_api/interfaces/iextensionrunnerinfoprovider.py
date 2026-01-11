import pathlib
from typing import Protocol


class IExtensionRunnerInfoProvider(Protocol):
    def get_current_env_name(self) -> str: ...

    def get_cache_dir_path(self) -> pathlib.Path: ...

    def get_venv_dir_path_of_env(self, env_name: str) -> pathlib.Path: ...

    def get_current_venv_dir_path(self) -> pathlib.Path: ...

    def get_venv_site_packages(
        self, venv_dir_path: pathlib.Path
    ) -> list[pathlib.Path]: ...

    def get_venv_python_interpreter(
        self, venv_dir_path: pathlib.Path
    ) -> pathlib.Path: ...
