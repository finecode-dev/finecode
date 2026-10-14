import asyncio
import collections.abc
import os
import pathlib

from finecode_extension_api.interfaces import ifilemanager

from fine_inspect_code.ratchet_record import RecordRefusedError
from fine_inspect_code.ratchet_snapshot import (
    BaselineSnapshotInvalidError,
    Snapshot,
    parse_snapshot,
)

__all__ = ["read_snapshots", "read_snapshots_or_refuse", "write_snapshot_text"]


async def read_snapshots(
    file_manager: ifilemanager.IFileManager,
    paths: collections.abc.Iterable[pathlib.Path],
) -> dict[pathlib.Path, Snapshot | None]:
    snapshots: dict[pathlib.Path, Snapshot | None] = {}
    for path in paths:
        try:
            content = await file_manager.get_content(path)
        except ifilemanager.FileNotFound:
            snapshots[path] = None
            continue
        except UnicodeDecodeError as error:
            raise BaselineSnapshotInvalidError(
                f"invalid diagnostics baseline snapshot {path}: {error}"
            ) from error
        snapshots[path] = parse_snapshot(content, path)
    return snapshots


async def read_snapshots_or_refuse(
    file_manager: ifilemanager.IFileManager,
    paths: collections.abc.Iterable[pathlib.Path],
    operation: str,
) -> dict[pathlib.Path, Snapshot | None]:
    try:
        return await read_snapshots(file_manager, paths)
    except BaselineSnapshotInvalidError as error:
        raise RecordRefusedError(
            f"cannot {operation} diagnostics baseline: {error}. Resolve the "
            "conflict by hand or take one side with git "
            "(git checkout --theirs|--ours <snapshot file>), then re-record "
            "on Linux with record_diagnostics_baseline and re-merge other "
            "platforms with merge_diagnostics_baseline from the CI results files."
        ) from error


async def write_snapshot_text(path: pathlib.Path, text: str) -> bool:
    data = text.encode("utf-8")
    try:
        current = await asyncio.to_thread(path.read_bytes)
    except FileNotFoundError:
        current = None
    if current == data:
        return False
    await asyncio.to_thread(_write_atomically, path, data)
    return True


def _write_atomically(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    _ = tmp.write_bytes(data)
    os.replace(tmp, path)
