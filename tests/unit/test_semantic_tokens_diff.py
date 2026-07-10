from __future__ import annotations

from finecode.lsp_server.endpoints.semantic_tokens import _diff_semantic_tokens_data


def test_identical_arrays_produce_no_edits() -> None:
    data = [0, 0, 5, 1, 0, 1, 2, 3, 2, 0]

    assert _diff_semantic_tokens_data(data, data) == []


def test_pure_insertion_produces_single_edit_with_new_data() -> None:
    old_data = [0, 0, 5, 1, 0]
    new_token = [1, 0, 3, 2, 0]
    new_data = old_data + new_token

    edits = _diff_semantic_tokens_data(old_data, new_data)

    assert edits == [{"start": 5, "deleteCount": 0, "data": new_token}]


def test_pure_deletion_produces_single_edit_with_no_data() -> None:
    kept_token = [0, 0, 5, 1, 0]
    removed_token = [1, 0, 3, 2, 0]
    old_data = kept_token + removed_token
    new_data = kept_token

    edits = _diff_semantic_tokens_data(old_data, new_data)

    assert edits == [{"start": 5, "deleteCount": 5}]


def test_replacement_produces_single_edit_with_replacement_data() -> None:
    old_data = [0, 0, 5, 1, 0]
    new_data = [0, 0, 5, 2, 1]

    edits = _diff_semantic_tokens_data(old_data, new_data)

    assert edits == [{"start": 0, "deleteCount": 5, "data": new_data}]


def test_empty_old_array_populates_everything() -> None:
    new_data = [0, 0, 5, 1, 0, 1, 2, 3, 2, 0]

    edits = _diff_semantic_tokens_data([], new_data)

    assert edits == [{"start": 0, "deleteCount": 0, "data": new_data}]
