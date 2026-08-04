from __future__ import annotations

import pytest
from finecode_extension_runner.service_config import ServiceConfigResolver
from finecode_extension_runner.service_names import derive_service_name


class IHttpClient:
    pass


class IRepositoryCredentialsProvider:
    pass


@pytest.mark.parametrize(
    ("interface", "expected_name"),
    [
        ("finecode_extension_api.interfaces.ihttpclient.IHttpClient", "http_client"),
        (
            (
                "finecode_extension_api.interfaces.irepositorycredentialsprovider."
                "IRepositoryCredentialsProvider"
            ),
            "repository_credentials_provider",
        ),
        ("fine_tasks.IForgeCredentialsProvider", "forge_credentials_provider"),
        # Derivation uses only the final segment, so a bare class name -- all the
        # ER has for an activator-registered binding -- resolves the same alias.
        ("IHttpClient", "http_client"),
        # A run of capitals is one word: `h_t_t_p_client` is unguessable when
        # writing the env var.
        ("IHTTPClient", "http_client"),
        ("IJsonRpcClient", "json_rpc_client"),
        ("IPyPackageLayoutInfoProvider", "py_package_layout_info_provider"),
        ("ICache", "cache"),
    ],
)
def test_derive_service_name_from_interface(interface: str, expected_name: str) -> None:
    assert derive_service_name(interface) == expected_name


def test_declared_config_reaches_a_binding_that_passed_none() -> None:
    # The binding is an activator's: it passes no config of its own, and must
    # still pick up a `[[tool.finecode.service]]` entry naming its interface.
    resolver = ServiceConfigResolver(
        declared_config_by_interface={IHttpClient: {"timeout": 30}},
        overrides_by_name={},
    )

    assert resolver.resolve(IHttpClient, None) == {"timeout": 30}


def test_override_reaches_a_binding_with_no_declaration_at_all() -> None:
    # Nothing is declared for this interface anywhere -- an activator bound it.
    # It is still configurable (ADR-0070, rule S-207).
    resolver = ServiceConfigResolver(
        declared_config_by_interface={},
        overrides_by_name={"http_client": {"timeout": 5}},
    )

    assert resolver.resolve(IHttpClient, None) == {"timeout": 5}
    assert resolver.unmatched_names() == []


def test_override_wins_over_declaration_which_wins_over_registration() -> None:
    resolver = ServiceConfigResolver(
        declared_config_by_interface={IHttpClient: {"timeout": 30, "retries": 2}},
        overrides_by_name={"http_client": {"timeout": 5}},
    )

    effective = resolver.resolve(IHttpClient, {"timeout": 1, "base_url": "http://x"})

    assert effective == {"timeout": 5, "retries": 2, "base_url": "http://x"}


def test_override_deep_merges_and_keeps_sibling_entries() -> None:
    resolver = ServiceConfigResolver(
        declared_config_by_interface={
            IRepositoryCredentialsProvider: {
                "credentials_by_repository": {
                    "pypi": {"username": "a", "password": "p1"},
                    "testpypi": {"username": "b", "password": "p2"},
                }
            }
        },
        overrides_by_name={
            "repository_credentials_provider": {
                "credentials_by_repository": {"testpypi": {"password": "secret"}}
            }
        },
    )

    effective = resolver.resolve(IRepositoryCredentialsProvider, None)

    assert effective == {
        "credentials_by_repository": {
            "pypi": {"username": "a", "password": "p1"},
            # username survives; only password was overridden
            "testpypi": {"username": "b", "password": "secret"},
        }
    }


def test_binding_with_neither_declaration_nor_override_is_left_untouched() -> None:
    resolver = ServiceConfigResolver({}, {})

    assert resolver.resolve(IHttpClient, None) is None
    assert resolver.resolve(IHttpClient, {"a": 1}) == {"a": 1}


def test_unmatched_override_is_reported() -> None:
    resolver = ServiceConfigResolver({}, {"no_such_service": {"x": 1}})

    resolver.resolve(IHttpClient, None)

    assert resolver.unmatched_names() == ["no_such_service"]


def test_colliding_aliases_are_reported_only_when_an_override_addresses_them() -> None:
    class OtherIHttpClient:
        pass

    OtherIHttpClient.__name__ = "IHttpClient"

    quiet = ServiceConfigResolver({}, {})
    quiet.resolve(IHttpClient, None)
    quiet.resolve(OtherIHttpClient, None)
    assert quiet.ambiguous_names() == {}

    loud = ServiceConfigResolver({}, {"http_client": {"timeout": 5}})
    loud.resolve(IHttpClient, None)
    loud.resolve(OtherIHttpClient, None)
    assert list(loud.ambiguous_names()) == ["http_client"]
