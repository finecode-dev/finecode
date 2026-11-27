# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectinfoprovider
from finecode_extension_api.phase_timing import synchronous_phase

from fine_envs import dump_config_action
from fine_envs.dump_config_render import render_config_dump


@dataclasses.dataclass
class DumpConfigHandlerConfig(code_action.ActionHandlerConfig): ...


class DumpConfigHandler(
    code_action.ActionHandler[
        dump_config_action.DumpConfigAction, DumpConfigHandlerConfig
    ]
):
    """Strip the keys config resolution already consumed and render the dump.

    Every later handler in the pipeline works on ``config_dump_content``, so the
    serialization format of the dump is decided here and nowhere else.
    """

    def __init__(
        self, project_info_provider: iprojectinfoprovider.IProjectInfoProvider
    ) -> None:
        self.project_info_provider = project_info_provider

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
        # extra gates are resolved into `extends` the same way presets are; a
        # re-processed dump must not re-activate a gate without the selection
        # file that authorised it.
        if "extra" in finecode_config:
            del finecode_config["extra"]

        active_selection = (
            await self.project_info_provider.get_workspace_extra_selection()
        )
        raw_config_dump = run_context.raw_config_dump
        with synchronous_phase(
            "render_config_dump",
            input_counts=lambda: {
                "top_level_keys": len(raw_config_dump),
                "selected_dependencies": len(active_selection),
                "selected_extras": sum(
                    len(extras) for extras in active_selection.values()
                ),
            },
        ) as phase:
            content = render_config_dump(raw_config_dump, active_selection)
            phase.completion_counts(lambda: {"output_chars": len(content)})
        run_context.config_dump_content = content

        return dump_config_action.DumpConfigRunResult(
            config_dump=run_context.raw_config_dump
        )
