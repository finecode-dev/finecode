import sys

import loguru

# if sys.version_info < (3, 12):
#     from typing_extensions import override
# else:
#     from typing import override

# from finecode.extension_runner.interfaces import ilogger


# class LoguruLogger(ilogger.ILogger):
#     def __init__(self) -> None:
#         # support non-unicode symbols:
#         # https://loguru.readthedocs.io/en/stable/resources/recipes.html
#         # #resolving-unicodeencodeerror-and-other-encoding-issues
#         sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

#     @override
#     def info(self, message: str) -> None:
#         loguru.logger.info(message)

#     @override
#     def debug(self, message: str) -> None:
#         loguru.logger.debug(message)

#     @override
#     def disable(self, package: str) -> None:
#         loguru.logger.disable(package)

#     @override
#     def enable(self, package: str) -> None:
#         loguru.logger.enable(package)


def get_logger():
    # we could implement ilogger.ILogger interface and call loguru methods inside, but
    # loguru takes caller name from `__name__`, so it would be then always this module.
    # To fix this behavior additional patching with finding the right name of module
    # would be required. To avoid this for now just return loguru instance as instance
    # of `ilogger.ILogger` (they are structurally compatible).

    # support non-unicode symbols: https://loguru.readthedocs.io/en/stable/resources/
    #               recipes.html#resolving-unicodeencodeerror-and-other-encoding-issues
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    return loguru.logger
