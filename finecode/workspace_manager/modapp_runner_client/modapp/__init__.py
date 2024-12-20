from modapp.client import BaseChannel, Stream
from modapp.models.dataclass import DataclassModel


class ModappServiceCls:
    async def keep_running_until_disconnect(
        self, channel: BaseChannel, request: DataclassModel
    ) -> Stream[DataclassModel]:
        return await channel.send_unary_stream(
            "/modapp.ModappService/KeepRunningUntilDisconnect",
            request,
            DataclassModel,
        )

ModappService = ModappServiceCls()
