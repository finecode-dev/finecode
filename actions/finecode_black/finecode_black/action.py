from __future__ import annotations
from typing import TYPE_CHECKING

from black import reformat_one, WriteBack
from black.concurrency import reformat_many
from black.mode import Mode, TargetVersion
from black.report import Report
from finecode import CodeAction

if TYPE_CHECKING:
    from pathlib import Path


# TODO: run with a given config
class BlackCodeAction(CodeAction):
    def run(self, apply_on: Path) -> None:
        target_version = TargetVersion.PY311  # TODO: config
        if target_version:
            versions = set([target_version])
        else:
            # We'll autodetect later.
            versions = set()
        mode = Mode(
            target_versions=versions,
            line_length=99,
            is_pyi=False,
            is_ipynb=False,
            skip_source_first_line=False,
            string_normalization=True,
            magic_trailing_comma=False,
            experimental_string_processing=False,
            preview=True,
            python_cell_magics=set(), # set(python_cell_magics),
        )
        report = Report(check=False, diff=False, quiet=True, verbose=True)
        
        reformat_one(src=apply_on, fast=False, write_back=WriteBack.YES, mode=mode, report=report)
        print('run black on', apply_on)
