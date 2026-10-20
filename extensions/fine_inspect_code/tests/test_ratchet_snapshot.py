"""Tests that the committed snapshot is deterministic and strictly validated.

The snapshot is a reviewed file: recording twice must yield identical bytes,
each platform's counts must be stored exactly, and a malformed file — a merge
conflict, a wrong type, an unknown version — must fail loudly as a named
snapshot error rather than cancelling sibling projects with a bare
``TypeError``.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from fine_inspect_code.ratchet_matching import FindingIdentity
from fine_inspect_code.ratchet_snapshot import (
    BaselineSnapshotInvalidError,
    PartitionSnapshot,
    Snapshot,
    parse_snapshot,
    render_snapshot,
    replace_partitions,
)

_PATH = pathlib.Path("/ws/proj/finecode-diagnostics-baseline.json")


def _identity(
    path: str = "a.py",
    source: str = "ruff",
    code: str = "F401",
    message: str = "unused import",
    contributor: str = "lint",
) -> FindingIdentity:
    return FindingIdentity(
        contributor=contributor, path=path, source=source, code=code, message=message
    )


def _snapshot() -> Snapshot:
    return Snapshot(
        contributors={
            "lint": PartitionSnapshot(
                platforms=("linux",),
                counts_by_platform={
                    "linux": {_identity(): 2, _identity(path="b.py"): 1}
                },
            )
        }
    )


def test_render_is_deterministic_and_ends_with_newline() -> None:
    first = render_snapshot(_snapshot())

    assert render_snapshot(_snapshot()) == first
    assert first.endswith("\n") and not first.endswith("\n\n")
    assert "\r" not in first
    parsed = json.loads(first)
    assert list(parsed["contributors"]) == ["lint"]
    assert list(parsed["contributors"]["lint"]["paths"]) == ["a.py", "b.py"]
    assert parsed["contributors"]["lint"]["paths"]["a.py"] == [
        {"source": "ruff", "code": "F401", "message": "unused import", "count": 2}
    ]


def test_uniform_counts_compact_to_count() -> None:
    snapshot = Snapshot(
        contributors={
            "lint": PartitionSnapshot(
                platforms=("darwin", "linux"),
                counts_by_platform={
                    "darwin": {_identity(): 3},
                    "linux": {_identity(): 3},
                },
            )
        }
    )

    entries = json.loads(render_snapshot(snapshot))["contributors"]["lint"]["paths"][
        "a.py"
    ]

    assert entries == [
        {"source": "ruff", "code": "F401", "message": "unused import", "count": 3}
    ]


def test_divergent_counts_list_nonzero_platforms() -> None:
    snapshot = Snapshot(
        contributors={
            "lint": PartitionSnapshot(
                platforms=("darwin", "linux"),
                counts_by_platform={
                    "darwin": {_identity(): 0},
                    "linux": {_identity(): 2},
                },
            )
        }
    )

    entries = json.loads(render_snapshot(snapshot))["contributors"]["lint"]["paths"][
        "a.py"
    ]

    assert entries == [
        {
            "source": "ruff",
            "code": "F401",
            "message": "unused import",
            "counts": {"linux": 2},
        }
    ]


def test_all_zero_entries_are_dropped() -> None:
    snapshot = Snapshot(
        contributors={
            "lint": PartitionSnapshot(
                platforms=("linux",),
                counts_by_platform={"linux": {_identity(): 0}},
            )
        }
    )

    parsed = json.loads(render_snapshot(snapshot))

    assert parsed["contributors"]["lint"]["paths"] == {}


def test_replace_partitions_touches_only_the_named_platform() -> None:
    snapshot = Snapshot(
        contributors={
            "lint": PartitionSnapshot(
                platforms=("linux",),
                counts_by_platform={"linux": {_identity(): 2}},
            ),
            "type_check": PartitionSnapshot(
                platforms=("linux",),
                counts_by_platform={"linux": {_identity("b.py", "pyrefly", "x", "m", "type_check"): 1}},
            ),
        }
    )

    replaced = replace_partitions(snapshot, "darwin", {"lint": {_identity(): 5}})

    assert replaced.contributors["type_check"] is snapshot.contributors["type_check"]
    assert replaced.contributors["lint"].platforms == ("darwin", "linux")
    assert replaced.contributors["lint"].counts_by_platform["linux"] == {
        _identity(): 2
    }
    assert replaced.contributors["lint"].counts_by_platform["darwin"] == {
        _identity(): 5
    }
    rendered = json.loads(render_snapshot(replaced))
    assert rendered["contributors"]["type_check"]["platforms"] == ["linux"]
    assert rendered["contributors"]["lint"]["platforms"] == ["darwin", "linux"]


def test_replace_partitions_adds_a_new_contributor() -> None:
    replaced = replace_partitions(None, "linux", {"lint": {_identity(): 1}})

    assert replaced.contributors["lint"].platforms == ("linux",)
    assert "lint" in json.loads(render_snapshot(replaced))["contributors"]


def test_parse_round_trip() -> None:
    assert parse_snapshot(render_snapshot(_snapshot()), _PATH) == _snapshot()


def test_parse_accepts_paths_as_posix_strings() -> None:
    text = json.dumps(
        {
            "version": 1,
            "contributors": {
                "lint": {
                    "platforms": ["linux"],
                    "paths": {
                        "sub/dir/a.py": [
                            {
                                "source": "ruff",
                                "code": "F401",
                                "message": "unused import",
                                "count": 1,
                            }
                        ]
                    },
                }
            },
        }
    )

    snapshot = parse_snapshot(text, _PATH)

    assert snapshot.contributors["lint"].counts_by_platform["linux"] == {
        _identity(path="sub/dir/a.py"): 1
    }


@pytest.mark.parametrize(
    "text",
    [
        b"\xff\xfe\x00invalid utf-8",
        "<<ibble<<<<< HEAD\n{}\n=======\n{}\n>>>>>>> other",
        json.dumps({"version": 2, "contributors": {}}),
        json.dumps({"version": 1, "contributors": []}),
        json.dumps({"version": 1, "contributors": {"lint": {"platforms": "linux"}}}),
        json.dumps(
            {
                "version": 1,
                "contributors": {
                    "lint": {"platforms": ["linux"], "paths": {"a.py": [{"source": "ruff", "code": "F", "message": 3, "count": 1}]}}
                },
            }
        ),
        json.dumps(
            {
                "version": 1,
                "contributors": {
                    "lint": {"platforms": ["linux"], "paths": {"a.py": [{"source": "ruff", "code": "F", "message": "m", "count": "x"}]}}
                },
            }
        ),
        json.dumps(
            {
                "version": 1,
                "contributors": {
                    "lint": {
                        "platforms": ["linux"],
                        "paths": {"a.py": [{"source": "r", "code": "c", "message": "m", "count": 1, "counts": {"linux": 1}}]},
                    }
                },
            }
        ),
        json.dumps(
            {
                "version": 1,
                "contributors": {
                    "lint": {"platforms": ["linux"], "paths": {"a.py": [{"source": "r", "code": "c", "message": "m"}]}}
                },
            }
        ),
        json.dumps(
            {
                "version": 1,
                "contributors": {
                    "lint": {"platforms": ["linux"], "paths": {"a.py": [{"source": "r", "code": "c", "message": "m", "counts": {"darwin": 1}}]}}
                },
            }
        ),
        "[]",
        "{not json",
    ],
    ids=[
        "invalid-utf8-bytes",
        "conflict-markers",
        "unknown-version",
        "contributors-not-object",
        "platforms-not-list",
        "message-not-string",
        "count-not-int",
        "both-count-and-counts",
        "neither-count-nor-counts",
        "counts-unknown-platform",
        "top-level-not-object",
        "not-json",
    ],
)
def test_invalid_snapshots_raise_only_snapshot_error(text: str | bytes) -> None:
    with pytest.raises(BaselineSnapshotInvalidError) as error:
        _ = parse_snapshot(text, _PATH)

    assert str(_PATH) in str(error.value)
    assert not isinstance(
        error.value, (TypeError, KeyError, AttributeError, UnicodeDecodeError)
    )
