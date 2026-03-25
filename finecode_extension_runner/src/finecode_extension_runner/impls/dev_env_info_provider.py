# import pathlib
# import typing

# from finecode_extension_api import common_types
from finecode_extension_api.interfaces import idevenvinfoprovider, ilogger


class DevEnvInfoProvider(
    idevenvinfoprovider.IDevEnvInfoProvider
):
    def __init__(
        self,
        logger: ilogger.ILogger,
        # docs_owned_by_ide: list[str],
        # get_document_func: typing.Callable,
        # save_document_func: typing.Callable,
    ) -> None:
        self.logger = logger
        # self.docs_owned_by_ide = docs_owned_by_ide
        # self.get_document_func = get_document_func
        # self.save_document_func = save_document_func

    # async def owned_files(self, dev_env: common_types.DevEnv) -> list[pathlib.Path]:
    #     ...

    # async def is_owner_of(self, dev_env: common_types.DevEnv, file_path: pathlib.Path) -> bool:
    #     ...

    # async def file_is_owned_by(self, file_path: pathlib.Path) -> list[common_types.DevEnv]:
    #     ...

    # async def files_owned_by_dev_envs(self) -> list[pathlib.Path]:
    #     ...

    # async def get_file_content(self, file_path: pathlib.Path) -> bytes:
    #     ...
    
    # async def save_file_content(self, file_path: pathlib.Path, file_content: bytes) -> None:
    #     ...
