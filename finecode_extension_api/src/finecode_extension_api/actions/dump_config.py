import dataclasses
import pathlib
import pprint
import sys
import typing

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from finecode_extension_api import code_action, textstyler


@dataclasses.dataclass
class DumpConfigRunPayload(code_action.RunActionPayload):
    # `source_file_path` is not for reading, config is already read and its content is
    # in `project_raw_config`, but for providing config path to allow for example to
    # resolve relative pathes in project config
    source_file_path: pathlib.Path
    project_raw_config: dict[str, typing.Any]
    target_file_path: pathlib.Path


class DumpConfigRunContext(code_action.RunActionContext):
    def __init__(
        self,
        run_id: int,
    ) -> None:
        super().__init__(run_id=run_id)

        self.raw_config_dump: dict[str, typing.Any] = {}

    async def init(self, initial_payload: DumpConfigRunPayload) -> None:
        self.raw_config_dump = initial_payload.project_raw_config


@dataclasses.dataclass
class DumpConfigRunResult(code_action.RunActionResult):
    config_dump: dict[str, typing.Any]

    @override
    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, DumpConfigRunResult):
            return

        self.config_dump = other.config_dump

    def to_text(self) -> str | textstyler.StyledText:
        formatted_dump_str = pprint.pformat(self.config_dump)
        return formatted_dump_str


class DumpConfigAction(code_action.Action):
    PAYLOAD_TYPE = DumpConfigRunPayload
    RUN_CONTEXT_TYPE = DumpConfigRunContext
    RESULT_TYPE = DumpConfigRunResult
