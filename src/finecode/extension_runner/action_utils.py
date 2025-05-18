import hashlib
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple


@contextmanager
def permanent_or_tmp_file_path(file_path: Path | None, file_content: str):
    if file_path is not None:
        yield file_path
    else:
        with tempfile.NamedTemporaryFile() as tmp_file:
            tmp_file.write(file_content.encode("utf-8"))
            yield Path(tmp_file.name)


@contextmanager
def tmp_file_copy_path(file_path: Path | None, file_content: str):
    # the same extension is important, because some tools like black check file
    # extension as well
    with tempfile.NamedTemporaryFile(
        suffix=file_path.suffix if file_path is not None else None
    ) as tmp_file:
        if file_content != "":
            tmp_file.write(file_content.encode("utf-8"))
            tmp_file.flush()
        elif file_path is not None:
            with open(file_path, "rb") as original_file:
                tmp_file.write(original_file.read())
                tmp_file.flush()
        else:
            raise ValueError("Invalid arguments")

        yield Path(tmp_file.name)


@contextmanager
def tmp_dir_copy_path(
    dir_path: Path, file_pathes_with_contents: list[tuple[Path | None, str]]
):
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_dir_path = Path(tmp_dir)
        tmp_pathes: list[Path] = []
        for file_path, file_content in file_pathes_with_contents:
            if file_content != "":
                # tmp_file.write(file_content.encode('utf-8'))
                # tmp_file.flush()
                raise NotImplementedError()
            elif file_path is not None:
                file_rel_path = file_path.relative_to(dir_path)
                tmp_path = tmp_dir_path / file_rel_path
                os.makedirs(tmp_path.parent, exist_ok=True)
                tmp_path.touch(exist_ok=True)
                tmp_pathes.append(tmp_path)
                with (
                    open(file_path, "rb") as original_file,
                    open(tmp_path, "wb") as tmp_file,
                ):
                    tmp_file.write(original_file.read())
                    tmp_file.flush()
            else:
                raise ValueError("Invalid arguments")

        yield tmp_dir_path, tmp_pathes


class FileVersion(NamedTuple):
    # currently we compare only the hash, but we could use the same approach as mypy and
    # compare first change time of the file and hash only if needed
    hash: str


def get_file_version(file_path: Path) -> FileVersion:
    with open(file_path, "rb") as file_obj:
        hash_value = hashlib.file_digest(file_obj, "blake2b").hexdigest()
    return FileVersion(
        hash=hash_value,
    )
