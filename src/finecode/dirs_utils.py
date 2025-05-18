from pathlib import Path
from typing import Sequence


def find_changed_dirs(
    new_dirs: Sequence[Path], old_dirs: Sequence[Path]
) -> tuple[list[Path], list[Path]]:
    added_dirs: list[Path] = []
    deleted_dirs: list[Path] = []
    for new_dir in new_dirs:
        if new_dir not in old_dirs:
            added_dirs.append(new_dir)
    for old_dir in old_dirs:
        if old_dir not in new_dirs:
            deleted_dirs.append(old_dir)

    return added_dirs, deleted_dirs
