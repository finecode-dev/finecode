import pathlib
from typing import Callable

from finecode_extension_api.interfaces import iextensionrunnerinfoprovider, ilogger


class ExtensionRunnerInfoProvider(
    iextensionrunnerinfoprovider.IExtensionRunnerInfoProvider
):
    def __init__(
        self, cache_dir_path_getter: Callable[[], pathlib.Path], logger: ilogger.ILogger
    ) -> None:
        self.cache_dir_path_getter = cache_dir_path_getter
        self.logger = logger

        self._site_packages_cache: dict[pathlib.Path, list[pathlib.Path]] = {}

    def get_cache_dir_path(self) -> pathlib.Path:
        return self.cache_dir_path_getter()

    def get_venv_dir_path_of_env(self, env_name: str) -> pathlib.Path:
        cache_dir_path = self.get_cache_dir_path()
        # assume cache dir is directly in venv
        current_venv_dir_path = cache_dir_path.parent
        venvs_dir_path = current_venv_dir_path.parent
        return venvs_dir_path / env_name

    def get_venv_site_packages(self, venv_dir_path: pathlib.Path) -> list[pathlib.Path]:
        # venv site packages can be cached because they don't change and if user runs
        # prepare-envs or updates environment in any other way, current ER should be
        # reloaded and cache will be automatically cleared
        if venv_dir_path in self._site_packages_cache:
            return self._site_packages_cache[venv_dir_path]

        site_packages: list[pathlib.Path] = []
        for lib_dir_name in ["lib", "lib64"]:
            lib_dir_path = venv_dir_path / lib_dir_name

            if not lib_dir_path.exists():
                continue

            # assume there is only one python version in venv
            lib_python_dir_path = next(
                dir_path
                for dir_path in lib_dir_path.iterdir()
                if dir_path.is_dir() and dir_path.name.startswith("python")
            )
            site_packages_path = lib_python_dir_path / "site-packages"
            if site_packages_path.exists():
                site_packages.append(site_packages_path)
            else:
                self.logger.warning(
                    f"site-packages directory expected in {lib_python_dir_path}, but wasn't exist. Venv seems to be invalid"
                )

        self._site_packages_cache[venv_dir_path] = site_packages
        return site_packages
