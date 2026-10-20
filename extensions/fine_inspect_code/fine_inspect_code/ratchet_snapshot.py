from __future__ import annotations

import dataclasses
import json
import pathlib
import typing

from finecode_extension_api import code_action

from fine_inspect_code.ratchet_matching import FindingIdentity

__all__ = [
    "SNAPSHOT_VERSION",
    "BaselineSnapshotInvalidError",
    "ContributorPartition",
    "PartitionSnapshot",
    "Snapshot",
    "SnapshotEntry",
    "SnapshotFile",
    "parse_snapshot",
    "render_snapshot",
    "replace_partitions",
]

SNAPSHOT_VERSION = 1


class SnapshotEntry(typing.TypedDict):
    source: str
    code: str
    message: str
    count: typing.NotRequired[int]
    counts: typing.NotRequired[dict[str, int]]


class ContributorPartition(typing.TypedDict):
    platforms: list[str]
    paths: dict[str, list[SnapshotEntry]]


class SnapshotFile(typing.TypedDict):
    version: int
    contributors: dict[str, ContributorPartition]


@dataclasses.dataclass(frozen=True)
class PartitionSnapshot:
    platforms: tuple[str, ...]
    counts_by_platform: dict[str, dict[FindingIdentity, int]]


@dataclasses.dataclass(frozen=True)
class Snapshot:
    contributors: dict[str, PartitionSnapshot]


class BaselineSnapshotInvalidError(code_action.ActionFailedException):
    """The committed baseline snapshot cannot be read.

    Raised for malformed JSON, undecodable bytes, wrong value types, unknown
    versions and structural errors. It names the file; record and merge refuse
    with it, and the ratchet contains it as a visible error diagnostic scoped
    to the project instead of cancelling sibling projects.
    """


def _invalid(snapshot_path: pathlib.Path, detail: object) -> BaselineSnapshotInvalidError:
    return BaselineSnapshotInvalidError(
        f"invalid diagnostics baseline snapshot {snapshot_path}: {detail}"
    )


def _require(condition: bool, snapshot_path: pathlib.Path, detail: object) -> None:
    if not condition:
        raise _invalid(snapshot_path, detail)


def _parse_snapshot(
    text: str | bytes, snapshot_path: pathlib.Path
) -> Snapshot:
    if isinstance(text, bytes):
        text = text.decode("utf-8")
    data = json.loads(text)
    _require(isinstance(data, dict), snapshot_path, "top level must be an object")
    assert isinstance(data, dict)
    version = data.get("version")
    _require(
        type(version) is int and version == SNAPSHOT_VERSION,
        snapshot_path,
        f"unknown version {version!r}",
    )
    contributors_raw = data.get("contributors")
    _require(
        isinstance(contributors_raw, dict), snapshot_path, "'contributors' must be an object"
    )
    assert isinstance(contributors_raw, dict)
    contributors: dict[str, PartitionSnapshot] = {}
    for name, partition_raw in contributors_raw.items():
        _require(
            isinstance(name, str) and isinstance(partition_raw, dict),
            snapshot_path,
            "contributor partitions must be objects keyed by name",
        )
        assert isinstance(partition_raw, dict)
        platforms_raw = partition_raw.get("platforms")
        _require(
            isinstance(platforms_raw, list)
            and all(isinstance(p, str) for p in platforms_raw),
            snapshot_path,
            "'platforms' must be a list of strings",
        )
        assert isinstance(platforms_raw, list)
        platforms = tuple(platforms_raw)
        paths_raw = partition_raw.get("paths")
        _require(
            isinstance(paths_raw, dict), snapshot_path, "'paths' must be an object"
        )
        assert isinstance(paths_raw, dict)
        counts_by_platform: dict[str, dict[FindingIdentity, int]] = {
            platform: {} for platform in platforms
        }
        for relative_path, entries in paths_raw.items():
            _require(
                isinstance(relative_path, str) and isinstance(entries, list),
                snapshot_path,
                "paths must map a relative path to a list of entries",
            )
            assert isinstance(entries, list)
            for entry in entries:
                _require(isinstance(entry, dict), snapshot_path, "entry must be an object")
                assert isinstance(entry, dict)
                source = entry.get("source")
                code = entry.get("code")
                message = entry.get("message")
                _require(
                    isinstance(source, str)
                    and isinstance(code, str)
                    and isinstance(message, str),
                    snapshot_path,
                    "entry 'source', 'code' and 'message' must be strings",
                )
                assert isinstance(source, str)
                assert isinstance(code, str)
                assert isinstance(message, str)
                has_count = "count" in entry
                has_counts = "counts" in entry
                _require(
                    has_count != has_counts,
                    snapshot_path,
                    "entry must have exactly one of 'count' or 'counts'",
                )
                identity = FindingIdentity(
                    contributor=name,
                    path=relative_path,
                    source=source,
                    code=code,
                    message=message,
                )
                if has_count:
                    count = entry.get("count")
                    _require(
                        isinstance(count, int) and not isinstance(count, bool),
                        snapshot_path,
                        "entry 'count' must be an integer",
                    )
                    assert isinstance(count, int)
                    for platform in platforms:
                        counts_by_platform[platform][identity] = count
                else:
                    counts = entry.get("counts")
                    _require(
                        isinstance(counts, dict),
                        snapshot_path,
                        "entry 'counts' must be an object",
                    )
                    assert isinstance(counts, dict)
                    for platform, count in counts.items():
                        _require(
                            platform in platforms,
                            snapshot_path,
                            f"entry lists platform {platform!r} "
                            "outside the partition's platforms",
                        )
                        _require(
                            isinstance(count, int) and not isinstance(count, bool),
                            snapshot_path,
                            "entry 'counts' values must be integers",
                        )
                        assert isinstance(count, int)
                        counts_by_platform[platform][identity] = count
        contributors[name] = PartitionSnapshot(
            platforms=platforms, counts_by_platform=counts_by_platform
        )
    return Snapshot(contributors=contributors)


def parse_snapshot(text: str | bytes, snapshot_path: pathlib.Path) -> Snapshot:
    try:
        return _parse_snapshot(text, snapshot_path)
    except BaselineSnapshotInvalidError:
        raise
    except Exception as error:
        raise _invalid(snapshot_path, error) from error


def render_snapshot(snapshot: Snapshot) -> str:
    output: SnapshotFile = {"version": SNAPSHOT_VERSION, "contributors": {}}
    for name in sorted(snapshot.contributors):
        partition = snapshot.contributors[name]
        platforms = sorted(set(partition.platforms))
        per_identity: dict[tuple[str, str, str, str], dict[str, int]] = {}
        for platform in platforms:
            for identity, count in partition.counts_by_platform.get(
                platform, {}
            ).items():
                per_identity.setdefault(
                    (identity.path, identity.source, identity.code, identity.message),
                    {},
                )[platform] = count
        paths: dict[str, list[SnapshotEntry]] = {}
        for relative_path, source, code, message in sorted(per_identity):
            counts = per_identity[(relative_path, source, code, message)]
            values = [counts.get(platform, 0) for platform in platforms]
            if all(value == 0 for value in values):
                continue
            if platforms and all(value == values[0] for value in values):
                entry: SnapshotEntry = {
                    "source": source,
                    "code": code,
                    "message": message,
                    "count": values[0],
                }
            else:
                entry = {
                    "source": source,
                    "code": code,
                    "message": message,
                    "counts": {
                        platform: counts[platform]
                        for platform in platforms
                        if counts.get(platform, 0) != 0
                    },
                }
            paths.setdefault(relative_path, []).append(entry)
        output["contributors"][name] = {"platforms": platforms, "paths": paths}
    return json.dumps(output, indent=2, ensure_ascii=False) + "\n"


def replace_partitions(
    snapshot: Snapshot | None,
    platform: str,
    replacements: dict[str, dict[FindingIdentity, int]],
) -> Snapshot:
    contributors: dict[str, PartitionSnapshot] = (
        dict(snapshot.contributors) if snapshot is not None else {}
    )
    for name, counts in replacements.items():
        old = contributors.get(name)
        old_platforms = old.platforms if old is not None else ()
        old_counts: dict[str, dict[FindingIdentity, int]] = (
            old.counts_by_platform if old is not None else {}
        )
        updated_counts = dict(old_counts)
        updated_counts[platform] = dict(counts)
        contributors[name] = PartitionSnapshot(
            platforms=tuple(sorted(set(old_platforms) | {platform})),
            counts_by_platform=updated_counts,
        )
    return Snapshot(contributors=contributors)
