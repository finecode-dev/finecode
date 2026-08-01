from __future__ import annotations

import pytest

from finecode.cli_app.cli import parse_service_config_from_env


def test_flat_path_produces_single_level_param(monkeypatch: pytest.MonkeyPatch) -> None:
    # A service with no nested structure (e.g. a top-level token) should not
    # require the caller to invent an artificial nesting level.
    monkeypatch.setenv("FINECODE_SERVICE_CONFIG_HTTP_CLIENT__TIMEOUT", "30")

    overrides = parse_service_config_from_env()

    assert overrides == {"http_client": {"timeout": 30}}


def test_nested_path_builds_dict_at_each_segment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unlike the handler env-var format, service overrides must be able to
    # reach into arbitrarily nested config (e.g. a table keyed by repository
    # name) without flattening sibling keys away.
    monkeypatch.setenv(
        "FINECODE_SERVICE_CONFIG_REPOSITORY_CREDENTIALS_PROVIDER"
        "__CREDENTIALS_BY_REPOSITORY__TESTPYPI__PASSWORD",
        "secret-token",
    )

    overrides = parse_service_config_from_env()

    assert overrides == {
        "repository_credentials_provider": {
            "credentials_by_repository": {"testpypi": {"password": "secret-token"}}
        }
    }


def test_non_json_value_falls_back_to_raw_string(monkeypatch: pytest.MonkeyPatch) -> None:
    # A bare secret token (e.g. "ghp_xxx") is not valid JSON. The handler
    # parser hard-fails on this; the service parser must not, since the
    # secret field is the one people set most and quoting it as a JSON string
    # is an easy-to-miss trap.
    monkeypatch.setenv(
        "FINECODE_SERVICE_CONFIG_REPOSITORY_CREDENTIALS_PROVIDER__TOKEN",
        "ghp_not_valid_json",
    )

    overrides = parse_service_config_from_env()

    assert overrides == {
        "repository_credentials_provider": {"token": "ghp_not_valid_json"}
    }


def test_json_value_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINECODE_SERVICE_CONFIG_HTTP_CLIENT__RETRIES", "3")
    monkeypatch.setenv(
        "FINECODE_SERVICE_CONFIG_HTTP_CLIENT__ENABLED", "true"
    )

    overrides = parse_service_config_from_env()

    assert overrides == {"http_client": {"retries": 3, "enabled": True}}


def test_missing_param_segment_is_skipped_with_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A var with only a service name and no param path names nothing to
    # override, so it must be dropped rather than silently misapplied.
    monkeypatch.setenv("FINECODE_SERVICE_CONFIG_HTTP_CLIENT", "1")

    overrides = parse_service_config_from_env()

    assert overrides == {}


def test_double_underscore_inside_intended_identifier_is_parsed_as_nesting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Service/param identifiers cannot contain "__": there is no way to
    # distinguish an identifier that happens to contain a double underscore
    # from an intended nesting boundary, so it is always treated as the
    # latter. This test documents that consequence rather than asserting it
    # is somehow avoided.
    monkeypatch.setenv("FINECODE_SERVICE_CONFIG_MY__SERVICE__PARAM", "1")

    overrides = parse_service_config_from_env()

    assert overrides == {"my": {"service": {"param": 1}}}
