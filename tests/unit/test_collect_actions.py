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
