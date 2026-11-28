"""Tests for finding native session files by a marker they contain."""

from __future__ import annotations

import pathlib

import pytest

from fine_agent import history_search
from fine_agent.history_search import file_contains, files_containing


def test_marker_split_across_chunks_is_found(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Session files are read in chunks; a marker straddling a chunk boundary
    must still match, or the session it identifies is reported missing."""
    monkeypatch.setattr(history_search, "_CHUNK_SIZE", 4)
    path = tmp_path / "s.jsonl"
    _ = path.write_text("xxxxxxfc-marker-123yyyy", encoding="utf-8")

    assert file_contains(path, "fc-marker-123") is True
    assert file_contains(path, "fc-marker-124") is False


def test_unreadable_files_are_reported_separately(tmp_path: pathlib.Path) -> None:
    hit = tmp_path / "hit.jsonl"
    _ = hit.write_text("... m-1 ...", encoding="utf-8")
    miss = tmp_path / "miss.jsonl"
    _ = miss.write_text("nothing", encoding="utf-8")
    gone = tmp_path / "gone.jsonl"

    found, unreadable = files_containing([hit, miss, gone], "m-1")

    assert found == [hit]
    assert unreadable == [gone]
