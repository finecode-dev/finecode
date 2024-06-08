from __future__ import annotations
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from black import reformat_one, WriteBack
from black.concurrency import reformat_many
from black.mode import Mode, TargetVersion
from black.report import Report
from finecode import CodeFormatAction, CodeActionConfig, FormatRunResult

if TYPE_CHECKING:
    from pathlib import Path


class BlackCodeActionConfig(CodeActionConfig):
    # TODO: should be set
    target_versions: list[
        # TODO: investigate why list of literals doesn't work
        # Literal["PY33", "PY34", "PY35", "PY36", "PY37", "PY38", "PY39", "PY310", "PY311", "PY312"]
        str
    ] = []
    line_length: int = 99
    preview: bool = False
    unstable: bool = False
    skip_string_normalization: bool = False
    skip_source_first_line: bool = False
    skip_magic_trailing_comma: bool = False
    python_cell_magics: bool = False  # it should be a set?


class BlackCodeAction(CodeFormatAction[BlackCodeActionConfig]):
    def run(self, apply_on: Path) -> FormatRunResult:
        logger.trace('black start')
        report = self.get_report()
        reformat_one(
            src=apply_on,
            fast=False,
            write_back=WriteBack.YES,
            mode=self.get_mode(),
            report=report,
        )
        logger.trace('black end')
        return FormatRunResult(changed=report.change_count > 0, code=None)

    def run_on_many(self, apply_on: list[Path]) -> dict[Path, FormatRunResult]:
        report = self.get_report()
        reformat_many(
            sources=set(apply_on),
            fast=False,
            write_back=WriteBack.YES,
            mode=self.get_mode(),
            report=report,
            workers=None,
        )
        # TODO: black report is generalized for all files, parse output?
        return {
            filepath: FormatRunResult(changed=True, code=None) for filepath in apply_on
        }

    def get_mode(self) -> Mode:
        return Mode(
            target_versions=set(
                [TargetVersion[ver] for ver in self.config.target_versions]
            ),
            line_length=self.config.line_length,
            is_pyi=False,
            is_ipynb=False,
            skip_source_first_line=self.config.skip_source_first_line,
            string_normalization=not self.config.skip_string_normalization,
            magic_trailing_comma=not self.config.skip_magic_trailing_comma,
            preview=self.config.preview,
            python_cell_magics=set(),  # set(python_cell_magics),
            unstable=self.config.unstable,
        )

    def get_report(self) -> Report:
        return Report(check=False, diff=False, quiet=True, verbose=True)


if __name__ == "__main__":
    import time
    a = BlackCodeAction(BlackCodeActionConfig())
    s = time.time()
    a.run(Path("/home/user/Development/FineCode/finecode/finecode/cli.py"))
    print("time: ", time.time() - s)
