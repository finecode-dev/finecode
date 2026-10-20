from __future__ import annotations

from typing import Any

from finecode.wm_server.config import collect_actions


def _build_config() -> dict[str, Any]:
    return {
        "tool": {
            "finecode": {
                "env": {
                    "testing@cpython-3.11": {"interpreter": "cpython@3.11"},
                    "dev_no_runtime": {},
                },
                "action": {
                    "test_action": {
                        "source": "test.actions.TestAction",
                        "handlers": [
                            {
                                "name": "matrix_handler",
                                "source": "test.handlers.MatrixHandler",
                                "env": "testing@cpython-3.11",
                            },
                            {
                                "name": "plain_handler",
                                "source": "test.handlers.PlainHandler",
                                "env": "dev_no_runtime",
                            },
                        ],
                    }
                },
            }
        }
    }


def test_collect_actions_sets_interpreter_from_env_table() -> None:
    config = _build_config()

    actions = collect_actions._collect_actions_in_config(config)

    assert len(actions) == 1
    action = actions[0]
    handlers_by_name = {handler.name: handler for handler in action.handlers}

    assert handlers_by_name["matrix_handler"].interpreter == "cpython@3.11"


def test_collect_actions_leaves_interpreter_none_for_non_interpreter_env() -> None:
    config = _build_config()

    actions = collect_actions._collect_actions_in_config(config)

    handlers_by_name = {handler.name: handler for handler in actions[0].handlers}

    assert handlers_by_name["plain_handler"].interpreter is None


def _build_services_config(services: list[dict[str, Any]]) -> dict[str, Any]:
    return {"tool": {"finecode": {"service": services}}}


def test_collect_services_keeps_entries_as_declared() -> None:
    config = _build_services_config(
        [
            {
                "interface": "finecode_extension_api.interfaces.ihttpclient.IHttpClient",
                "source": "finecode_httpclient.HttpClient",
                "env": "dev",
            }
        ]
    )

    services = collect_actions._collect_services_in_config(config)

    assert len(services) == 1
    assert services[0].interface == (
        "finecode_extension_api.interfaces.ihttpclient.IHttpClient"
    )
    assert services[0].source == "finecode_httpclient.HttpClient"


def test_collect_services_accepts_a_config_only_entry() -> None:
    # `source`/`env` are optional so an entry can layer config onto a binding an
    # activator owns, without restating (and pinning) the implementation.
    config = _build_services_config(
        [
            {
                "interface": "finecode_extension_api.interfaces.ihttpclient.IHttpClient",
                "config": {"timeout": 30},
            }
        ]
    )

    services = collect_actions._collect_services_in_config(config)

    assert services[0].source is None
    assert services[0].env is None
    assert services[0].config == {"timeout": 30}


def test_collect_services_does_not_reject_colliding_aliases() -> None:
    # Alias collisions are decided in the ER, which is the only layer that sees
    # activator-registered bindings too (ADR-0070).
    config = _build_services_config(
        [
            {
                "interface": "pkg_a.ihttpclient.IHttpClient",
                "source": "pkg_a.HttpClient",
                "env": "dev",
            },
            {
                "interface": "pkg_b.ihttpclient.IHttpClient",
                "source": "pkg_b.HttpClient",
                "env": "dev",
            },
        ]
    )

    services = collect_actions._collect_services_in_config(config)

    assert len(services) == 2
