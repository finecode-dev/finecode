from __future__ import annotations

import isort.main as isort_main
import isort.settings as isort_settings

import finecode.extension_runner.action_utils as action_utils
from finecode import (CodeActionConfig, CodeFormatAction, FormatRunPayload,
                      FormatRunResult)


class IsortCodeActionConfig(CodeActionConfig): ...


# TODO: run with a given config
class IsortCodeAction(CodeFormatAction[IsortCodeActionConfig]):
    async def run(self, payload: FormatRunPayload) -> FormatRunResult:
        # TODO: config
        changed: bool = False
        code: str | None = None
        with action_utils.tmp_file_copy_path(
            file_path=payload.apply_on, file_content=payload.apply_on_text
        ) as file_path:
            # seems like isort doesn't return whether file was changed or not, only whether it is
            # still incorrectly sorted and whether file was skipped, use finecode check instead
            initial_file_version = action_utils.get_file_version(file_path)
            # result = 
            isort_main.sort_imports(
                file_name=file_path.as_posix(),
                # is it possible without overwriting?
                config=isort_settings.Config(overwrite_in_place=True),
                check=False,
                ask_to_apply=False,
                show_diff=False,
                write_to_stdout=False,
                # extension=ext_format,
                # config_trie=config_trie,
            )

            new_file_version = action_utils.get_file_version(file_path)
            # result is not None and not result.skipped
            changed = new_file_version != initial_file_version
            if changed:
                with open(file_path, "r") as f:
                    code = f.read()
        return FormatRunResult(changed=changed, code=code)
