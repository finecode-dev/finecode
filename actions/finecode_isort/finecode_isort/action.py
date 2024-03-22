from __future__ import annotations
from typing import TYPE_CHECKING

import isort.main as isort_main
import isort.settings as isort_settings
from finecode import CodeFormatAction, FormatRunResult, CodeActionConfig

if TYPE_CHECKING:
    from pathlib import Path


class IsortCodeActionConfig(CodeActionConfig):
    ...

# TODO: run with a given config
class IsortCodeAction(CodeFormatAction):
    def run(self, apply_on: Path) -> FormatRunResult:
        # TODO: config
        isort_main.sort_imports(
            apply_on.as_posix(),
            config=isort_settings.Config(),
            check=False,
            ask_to_apply=False,
            show_diff=False,
            write_to_stdout=False,
            # extension=ext_format,
            # config_trie=config_trie,
        )
        return FormatRunResult(changed=True, code=None)

    # TODO: analyze whether run_many is needed
