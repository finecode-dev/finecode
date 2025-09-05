import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions import dump_config as dump_config_action


@dataclasses.dataclass
class DumpConfigHandlerConfig(code_action.ActionHandlerConfig): ...


class DumpConfigHandler(
    code_action.ActionHandler[
        dump_config_action.DumpConfigAction, DumpConfigHandlerConfig
    ]
):
    async def run(
        self,
        payload: dump_config_action.DumpConfigRunPayload,
        run_context: dump_config_action.DumpConfigRunContext,
    ) -> dump_config_action.DumpConfigRunResult:
        # presets are resolved, remove tool.finecode.presets key to avoid repeating
        # resolving if dump config is processed
        finecode_config = run_context.raw_config_dump.get("tool", {}).get(
            "finecode", {}
        )
        if "presets" in finecode_config:
            del finecode_config["presets"]

        return dump_config_action.DumpConfigRunResult(
            config_dump=run_context.raw_config_dump
        )
