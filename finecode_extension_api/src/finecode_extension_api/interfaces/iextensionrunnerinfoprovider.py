import pathlib
from typing import Protocol


class IExtensionRunnerInfoProvider(Protocol):
    def get_cache_dir_path(self) -> pathlib.Path: ...

    def get_venv_dir_path_of_env(self, env_name: str) -> pathlib.Path: ...

    def get_venv_site_packages(
        self, venv_dir_path: pathlib.Path
    ) -> list[pathlib.Path]: ...
