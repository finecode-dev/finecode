import sys

import loguru
if sys.version_info < (3, 12):
    from typing_extensions import override
else:
    from typing import override

from finecode.extension_runner.interfaces import ilogger


class LoguruLogger(ilogger.ILogger):
    @override
    def info(self, message: str) -> None:
        loguru.logger.info(message)

    @override
    def debug(self, message: str) -> None:
        loguru.logger.debug(message)
    
    @override
    def disable(self, package: str) -> None:
        loguru.logger.disable(package)
    
    @override
    def enable(self, package: str) -> None:
        loguru.logger.enable(package)
