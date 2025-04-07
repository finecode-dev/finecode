from asyncio import BaseProtocol


class IProcessExecutor(BaseProtocol):
    async def submit(self, func, *args):
        # TODO: return type, typing of func and args
        ...
