from __future__ import annotations

import pytest

from finecode.cli_app.cli import parse_handler_config_from_env


def test_two_segments_produce_action_level_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two segments name an action and a param, with no handler in between, so
    # the override applies to every handler of the action -- keyed by "".
    monkeypatch.setenv("FINECODE_CONFIG_LINT__LINE_LENGTH", "100")

    overrides = parse_handler_config_from_env()

    assert overrides == {"lint": {"": {"line_length": 100}}}


def test_three_segments_produce_handler_specific_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FINECODE_CONFIG_LINT__RUFF__LINE_LENGTH", "120")

    overrides = parse_handler_config_from_env()

    assert overrides == {"lint": {"ruff": {"line_length": 120}}}


def test_json_values_are_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINECODE_CONFIG_LINT__RUFF__EXTEND_SELECT", '["B","I"]')
    monkeypatch.setenv("FINECODE_CONFIG_LINT__RUFF__FIX", "true")

    overrides = parse_handler_config_from_env()

    assert overrides == {"lint": {"ruff": {"extend_select": ["B", "I"], "fix": True}}}


def test_non_json_value_falls_back_to_raw_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A bare string value is not valid JSON. Requiring the user to wrap it in
    # JSON quotes (TARGET_VERSION='"py312"') is a trap, and neither the CLI
    # parser nor the service env parser imposes it, so this one must not
    # either.
    monkeypatch.setenv("FINECODE_CONFIG_LINT__RUFF__TARGET_VERSION", "py312")

    overrides = parse_handler_config_from_env()

    assert overrides == {"lint": {"ruff": {"target_version": "py312"}}}


def test_malformed_json_is_kept_as_string_rather_than_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Consequence of the fallback above: there is no way to tell a mistyped
    # JSON literal from a string that happens to start with "[", so it reaches
    # the handler as a string and fails there. This test documents that
    # trade-off rather than asserting it is avoided.
    monkeypatch.setenv("FINECODE_CONFIG_LINT__RUFF__EXTEND_SELECT", '["B","I"')

    overrides = parse_handler_config_from_env()

    assert overrides == {"lint": {"ruff": {"extend_select": '["B","I"'}}}


def test_missing_param_segment_is_skipped_with_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A var with only an action name and no param names nothing to override.
    monkeypatch.setenv("FINECODE_CONFIG_LINT", "1")

    overrides = parse_handler_config_from_env()

    assert overrides == {}


def test_segments_past_the_handler_are_flattened_into_one_param_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Unlike the service format, the handler format cannot nest: segment 2 is
    # always the handler, and everything after it is rejoined into a single
    # param name.
    monkeypatch.setenv("FINECODE_CONFIG_LINT__RUFF__A__B", "1")

    overrides = parse_handler_config_from_env()

    assert overrides == {"lint": {"ruff": {"a__b": 1}}}
