import copy
import pathlib
from typing import Any

import pytest

from finecode.wm_server.config import config_models
from finecode.wm_server.config.read_configs import (
    _merge_projects_configs,
    read_project_user_config,
    read_preset_config,
    resolve_interpreter_matrices,
)


def _write_toml(path: pathlib.Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# read_project_user_config
# ---------------------------------------------------------------------------


def test_project_user_config_absent_is_noop(tmp_path: pathlib.Path) -> None:
    """Absence of finecode-user.toml is a safe no-op.

    Users who have not created a personal config file must be unaffected — the
    shared configuration is used without any change or error.
    """
    result = read_project_user_config(tmp_path)
    assert result is None


def test_project_user_config_workspace_table_raises(tmp_path: pathlib.Path) -> None:
    """A [workspace] table in finecode-user.toml is rejected with a clear error.

    Workspace-scoped settings are shared by definition and belong in
    finecode-workspace.toml; allowing them in a personal file would create
    silent inconsistencies across developer machines.
    """
    _write_toml(tmp_path / "finecode-user.toml", "[workspace]\nfoo = 1\n")
    with pytest.raises(config_models.ConfigurationError, match="workspace"):
        read_project_user_config(tmp_path)


def test_project_user_config_malformed_raises(tmp_path: pathlib.Path) -> None:
    """A parse error in finecode-user.toml is a hard failure.

    A malformed personal config could silently drop all personal handlers,
    making the workspace appear healthy while personal tooling is missing.
    A hard fail surfaces the problem immediately.
    """
    (tmp_path / "finecode-user.toml").write_bytes(b"[bad toml\n")
    with pytest.raises(config_models.ConfigurationError, match="Failed to parse"):
        read_project_user_config(tmp_path)


def test_project_user_config_returns_flat_dict(tmp_path: pathlib.Path) -> None:
    """finecode-user.toml is parsed and returned as a plain flat dict.

    The flat schema (no [finecode] wrapper) is the contract for the user file;
    callers lift keys under tool.finecode before merging.
    """
    _write_toml(
        tmp_path / "finecode-user.toml",
        '[action.lint]\nhandlers = []\n',
    )
    result = read_project_user_config(tmp_path)
    assert result is not None
    assert "action" in result
    assert "lint" in result["action"]


# ---------------------------------------------------------------------------
# _merge_projects_configs — user config priority
# ---------------------------------------------------------------------------


def _make_project_config_with_handler(line_length: int) -> dict[str, Any]:
    return {
        "tool": {
            "finecode": {
                "action": {
                    "lint": {
                        "source": "myext.LintAction",
                        "handlers": [
                            {
                                "name": "ruff",
                                "source": "fine_python_ruff.RuffHandler",
                                "env": "dev_no_runtime",
                                "dependencies": [],
                                "config": {"line_length": line_length},
                            }
                        ],
                    }
                }
            }
        }
    }


def test_project_user_config_handler_override(tmp_path: pathlib.Path) -> None:
    """A handler config override in finecode-user.toml wins over project config.

    Personal preferences (e.g. a wider line length) must override shared values
    without requiring changes to committed config files.
    """
    project_config = _make_project_config_with_handler(line_length=88)

    user_config_raw: dict[str, Any] = {
        "action": {
            "lint": {
                "handlers": {
                    "ruff": {"config": {"line_length": 120}},
                }
            }
        }
    }
    wrapped_user: dict[str, Any] = {"tool": {"finecode": user_config_raw}}
    _merge_projects_configs(
        project_config,
        tmp_path / "pyproject.toml",
        wrapped_user,
        tmp_path / "finecode-user.toml",
    )

    handlers = project_config["tool"]["finecode"]["action"]["lint"]["handlers"]
    ruff = next(h for h in handlers if h["name"] == "ruff")
    assert ruff["config"]["line_length"] == 120


def test_project_user_config_dep_groups_merged(tmp_path: pathlib.Path) -> None:
    """User-declared dependency-groups are merged additively into project dep groups.

    A developer who needs a personal preset package in dev_workspace must be
    able to declare it without touching or losing the shared dependency list.
    """
    project_config: dict[str, Any] = {
        "tool": {"finecode": {}},
        "dependency-groups": {"dev_workspace": ["finecode~=0.3"]},
    }

    user_config_raw: dict[str, Any] = {
        "dependency-groups": {"dev_workspace": ["my_personal_preset>=1.0"]},
    }

    dep_groups: dict[str, list[Any]] = project_config.setdefault("dependency-groups", {})
    for group_name, packages in user_config_raw["dependency-groups"].items():
        if group_name not in dep_groups:
            dep_groups[group_name] = list(packages)
        else:
            for pkg in packages:
                if pkg not in dep_groups[group_name]:
                    dep_groups[group_name].append(pkg)

    dw = project_config["dependency-groups"]["dev_workspace"]
    assert "finecode~=0.3" in dw
    assert "my_personal_preset>=1.0" in dw


def test_project_user_config_new_action(tmp_path: pathlib.Path) -> None:
    """A new action declared in finecode-user.toml appears in the merged config.

    Personal actions (e.g. setup_dev_tools with a personal handler) must be
    available for `finecode run` without any committed config change.
    """
    project_config: dict[str, Any] = {"tool": {"finecode": {"action": {}}}}

    user_config_raw: dict[str, Any] = {
        "action": {
            "setup_dev_tools": {
                "source": "myext.SetupDevToolsAction",
                "handlers": [
                    {
                        "name": "copilot",
                        "source": "fine_vscode_ext.InstallExtHandler",
                        "env": "dev_no_runtime",
                        "dependencies": [],
                    }
                ],
            }
        }
    }
    wrapped_user: dict[str, Any] = {"tool": {"finecode": user_config_raw}}
    _merge_projects_configs(
        project_config,
        tmp_path / "pyproject.toml",
        wrapped_user,
        tmp_path / "finecode-user.toml",
    )

    actions = project_config["tool"]["finecode"]["action"]
    assert "setup_dev_tools" in actions
    assert actions["setup_dev_tools"]["source"] == "myext.SetupDevToolsAction"


# ---------------------------------------------------------------------------
# _merge_projects_configs — env.install_project (ADR-0046)
# ---------------------------------------------------------------------------


def test_preset_install_project_merges_into_existing_env_config(tmp_path: pathlib.Path) -> None:
    """A preset's install_project=true for an env survives merging into project config.

    Test-runner presets rely on this to make the project importable in the env
    their handler runs in — without it, tests fail with ModuleNotFoundError on
    first run for anyone who installs the preset.
    """
    project_config: dict[str, Any] = {
        "tool": {"finecode": {"env": {"dev": {"dependencies": {}}}}},
    }
    preset_config: dict[str, Any] = {
        "tool": {"finecode": {"env": {"dev": {"install_project": True}}}},
    }
    _merge_projects_configs(
        project_config,
        tmp_path / "pyproject.toml",
        preset_config,
        tmp_path / "preset.toml",
    )

    assert project_config["tool"]["finecode"]["env"]["dev"]["install_project"] is True


def test_project_config_disables_preset_install_project(tmp_path: pathlib.Path) -> None:
    """A project's own install_project=false overrides a preset's true.

    Consumers who install the project some other way must be able to opt back
    out of a preset's default (ADR-0046 rule 3) — otherwise they cannot avoid a
    duplicate or unwanted editable install.
    """
    project_config: dict[str, Any] = {
        "tool": {"finecode": {"env": {"dev": {"install_project": True}}}},
    }
    project_override: dict[str, Any] = {
        "tool": {"finecode": {"env": {"dev": {"install_project": False}}}},
    }
    _merge_projects_configs(
        project_config,
        tmp_path / "pyproject.toml",
        project_override,
        tmp_path / "pyproject.toml",
    )

    assert project_config["tool"]["finecode"]["env"]["dev"]["install_project"] is False


# ---------------------------------------------------------------------------
# _merge_projects_configs — env.interpreters (ADR-0047)
# ---------------------------------------------------------------------------


def test_interpreters_merge_survives_env_already_exists_from_preset(
    tmp_path: pathlib.Path,
) -> None:
    """A preset's `interpreters` axis for an env survives merging into an
    already-declared project env, mirroring `install_project` (ADR-0046).

    Without this merge rule, a matrix environment declared by a preset whose env
    is later touched again by another config layer would silently lose its
    `interpreters` axis, because only `dependencies`, `runner`, and
    `install_project` survive the "env already exists" merge path today.
    """
    project_config: dict[str, Any] = {
        "tool": {"finecode": {"env": {"testing": {"dependencies": {}}}}},
    }
    preset_config: dict[str, Any] = {
        "tool": {
            "finecode": {
                "env": {"testing": {"interpreters": ["cpython@3.11", "cpython@3.12"]}}
            }
        },
    }
    _merge_projects_configs(
        project_config,
        tmp_path / "pyproject.toml",
        preset_config,
        tmp_path / "preset.toml",
    )

    assert project_config["tool"]["finecode"]["env"]["testing"]["interpreters"] == [
        "cpython@3.11",
        "cpython@3.12",
    ]


def test_interpreters_merge_project_overrides_preset(tmp_path: pathlib.Path) -> None:
    """A project's own `interpreters` list wholesale-replaces a preset's list.

    Same override-wins rule as `install_project`: a consumer narrowing or
    widening the interpreter axis for their own project must not have their
    choice partially merged with the preset's declared list.
    """
    project_config: dict[str, Any] = {
        "tool": {
            "finecode": {
                "env": {"testing": {"interpreters": ["cpython@3.11", "cpython@3.12"]}}
            }
        },
    }
    project_override: dict[str, Any] = {
        "tool": {"finecode": {"env": {"testing": {"interpreters": ["pypy@3.11"]}}}},
    }
    _merge_projects_configs(
        project_config,
        tmp_path / "pyproject.toml",
        project_override,
        tmp_path / "pyproject.toml",
    )

    assert project_config["tool"]["finecode"]["env"]["testing"]["interpreters"] == [
        "pypy@3.11"
    ]


# ---------------------------------------------------------------------------
# resolve_interpreter_matrices (ADR-0047)
# ---------------------------------------------------------------------------


def _matrix_env_project_config() -> dict[str, Any]:
    """Worked-example-style fixture: a `testing` matrix environment expanding
    over three interpreters, with one `pytest` handler bound to it."""
    return {
        "tool": {
            "finecode": {
                "env": {
                    "testing": {
                        "interpreters": [
                            "cpython@3.11",
                            "cpython@3.12",
                            "pypy@3.11",
                        ],
                        "install_project": True,
                    }
                },
                "action": {
                    "run_tests": {
                        "source": "myext.RunTestsAction",
                        "handlers": [
                            {
                                "name": "pytest",
                                "source": "fine_pytest.PytestHandler",
                                "env": "testing",
                                "dependencies": [],
                            }
                        ],
                    }
                },
            }
        }
    }


_TESTING_CONCRETE_ENV_NAMES = {
    "testing@cpython-3.11",
    "testing@cpython-3.12",
    "testing@pypy-3.11",
}


def test_no_axis_config_is_untouched() -> None:
    """A project with no `interpreters` axis anywhere resolves byte-identically
    (PRD-0003 R7).

    Without this guarantee, every existing non-matrix project (the
    overwhelming majority today) would risk a spurious config change on
    upgrade, even though it never opted into the interpreter-matrix feature.
    """
    project_config: dict[str, Any] = {
        "tool": {"finecode": {"env": {"dev": {"dependencies": {}}}}},
    }
    before = copy.deepcopy(project_config)

    resolve_interpreter_matrices(project_config)

    assert project_config == before


def test_single_env_with_non_matrix_env_handler_is_untouched() -> None:
    """A single handler bound to a plain (non-matrix) env is left untouched.

    Guards against the expansion logic accidentally firing or rewriting a
    handler's `env` when no `interpreters` axis is declared anywhere in the
    project.
    """
    project_config: dict[str, Any] = {
        "tool": {
            "finecode": {
                "env": {"dev_no_runtime": {}},
                "action": {
                    "lint": {
                        "source": "myext.LintAction",
                        "handlers": [
                            {
                                "name": "ruff",
                                "source": "fine_python_ruff.RuffHandler",
                                "env": "dev_no_runtime",
                                "dependencies": [],
                            }
                        ],
                    }
                },
            }
        }
    }

    resolve_interpreter_matrices(project_config)

    env_table = project_config["tool"]["finecode"]["env"]
    assert set(env_table.keys()) == {"dev_no_runtime"}
    handler = project_config["tool"]["finecode"]["action"]["lint"]["handlers"][0]
    assert handler["env"] == "dev_no_runtime"


def test_matrix_env_expands_to_one_concrete_env_per_interpreter() -> None:
    """A matrix environment's env-table entry is replaced by one concrete
    child per interpreter, each carrying the matrix environment's other
    settings plus its own resolved `interpreter` identity.

    Without this, the matrix environment name would remain in
    `[tool.finecode.env]` as a phantom entry alongside its children instead of
    being replaced by them (ADR-0047).
    """
    project_config = _matrix_env_project_config()

    resolve_interpreter_matrices(project_config)

    env_table = project_config["tool"]["finecode"]["env"]
    assert "testing" not in env_table
    assert set(env_table.keys()) == _TESTING_CONCRETE_ENV_NAMES

    expected_interpreters = {
        "testing@cpython-3.11": "cpython@3.11",
        "testing@cpython-3.12": "cpython@3.12",
        "testing@pypy-3.11": "pypy@3.11",
    }
    for concrete_name, expected_interpreter in expected_interpreters.items():
        child = env_table[concrete_name]
        assert child["install_project"] is True
        assert child["interpreter"] == expected_interpreter
        assert "interpreters" not in child


def test_matrix_env_handler_ref_rewritten_to_concrete_children() -> None:
    """Every handler that referenced the matrix environment is rewritten into
    one entry per concrete child, preserving every other original field.

    Without this rewrite, a matrixed handler's `env` would still point at a
    now-deleted matrix environment name, and the handler would never run in
    any of the concrete per-interpreter envs it was meant to target.
    """
    project_config = _matrix_env_project_config()

    resolve_interpreter_matrices(project_config)

    handlers = project_config["tool"]["finecode"]["action"]["run_tests"]["handlers"]
    assert len(handlers) == 3
    assert {h["name"] for h in handlers} == {"pytest"}
    assert {h["env"] for h in handlers} == _TESTING_CONCRETE_ENV_NAMES
    for handler in handlers:
        assert handler["source"] == "fine_pytest.PytestHandler"


def test_matrix_env_dependency_group_copied_to_each_concrete_child() -> None:
    """Each concrete child receives its own copy of the matrix environment's
    fully-merged `dependency-groups` entry, not a shared reference.

    A shared reference would let a later per-child mutation (e.g. injecting
    the extension runner pin) leak across interpreter variants that must stay
    independent.
    """
    project_config = _matrix_env_project_config()
    project_config["dependency-groups"] = {
        "_test": ["pytest==8.*"],
        "testing": [{"include-group": "_test"}],
    }
    original_testing_group = project_config["dependency-groups"]["testing"]

    resolve_interpreter_matrices(project_config)

    deps_groups = project_config["dependency-groups"]
    assert "testing" not in deps_groups
    for concrete_name in _TESTING_CONCRETE_ENV_NAMES:
        assert deps_groups[concrete_name] == [{"include-group": "_test"}]
        assert deps_groups[concrete_name] is not original_testing_group


def test_matrix_env_disappears_from_dependency_groups() -> None:
    """The matrix environment's own key is removed from `dependency-groups`
    once its children are materialized.

    A matrix environment name left behind in `dependency-groups` would be
    discovered by `create_envs_discover_envs_handler`/
    `install_envs_discover_envs_handler` (which iterate `dependency-groups`
    keys directly) and built as an unwanted phantom extra virtualenv.
    """
    project_config = _matrix_env_project_config()
    project_config["dependency-groups"] = {"testing": []}
    assert "testing" in project_config["dependency-groups"]

    resolve_interpreter_matrices(project_config)

    assert "testing" not in project_config["dependency-groups"]


def test_matrix_env_with_no_explicit_deps_still_gets_discoverable_child_entries() -> None:
    """A matrix environment with no matching `dependency-groups` entry and no
    handler dependencies still produces a `[]` entry per concrete child.

    Without an entry (even an empty one), a concrete child with no
    dependencies of its own would never be discovered or created as an env by
    `create_envs`/`install_envs`.
    """
    project_config = _matrix_env_project_config()

    resolve_interpreter_matrices(project_config)

    deps_groups = project_config["dependency-groups"]
    for concrete_name in _TESTING_CONCRETE_ENV_NAMES:
        assert deps_groups[concrete_name] == []


def test_invalid_interpreter_string_raises_configuration_error() -> None:
    """A malformed interpreter string surfaces as `ConfigurationError`, the
    same error vocabulary used for every other malformed-config path in this
    file.

    An untranslated `InvalidInterpreterError` reaching a caller outside this
    module would be an undocumented exception type, a gap this project's
    conventions explicitly avoid.
    """
    project_config: dict[str, Any] = {
        "tool": {
            "finecode": {
                "env": {"testing": {"interpreters": ["not-a-valid-interpreter@@nope"]}},
                "action": {},
            }
        }
    }

    with pytest.raises(config_models.ConfigurationError):
        resolve_interpreter_matrices(project_config)


def test_mixed_matrix_raises_configuration_error() -> None:
    """An action mixing a matrix-environment handler with a
    single-interpreter-env handler is rejected with `ConfigurationError`.

    Silently allowing this would leave it ambiguous whether the
    single-interpreter handler should run once or once per interpreter,
    a contradiction ADR-0047 forbids outright.
    """
    project_config: dict[str, Any] = {
        "tool": {
            "finecode": {
                "env": {
                    "testing": {"interpreters": ["cpython@3.11", "cpython@3.12"]},
                    "dev": {},
                },
                "action": {
                    "run_tests": {
                        "source": "myext.RunTestsAction",
                        "handlers": [
                            {
                                "name": "pytest",
                                "source": "fine_pytest.PytestHandler",
                                "env": "testing",
                                "dependencies": [],
                            },
                            {
                                "name": "lint_check",
                                "source": "myext.LintCheckHandler",
                                "env": "dev",
                                "dependencies": [],
                            },
                        ],
                    }
                },
            }
        }
    }

    with pytest.raises(config_models.ConfigurationError, match="mixes"):
        resolve_interpreter_matrices(project_config)


def test_matrix_set_mismatch_raises_configuration_error() -> None:
    """Two matrix environments referenced by the same action with unequal
    interpreter sets are rejected with `ConfigurationError`.

    Without this check, a `run_tests` action spanning `testing` (3
    interpreters) and `stubs` (2 interpreters) would leave it undefined which
    interpreter's `stubs` handler should pair with the un-matched `testing`
    interpreter's handler.
    """
    project_config: dict[str, Any] = {
        "tool": {
            "finecode": {
                "env": {
                    "testing": {
                        "interpreters": ["cpython@3.11", "cpython@3.12", "pypy@3.11"]
                    },
                    "stubs": {"interpreters": ["cpython@3.11", "cpython@3.12"]},
                },
                "action": {
                    "run_tests": {
                        "source": "myext.RunTestsAction",
                        "handlers": [
                            {
                                "name": "pytest",
                                "source": "fine_pytest.PytestHandler",
                                "env": "testing",
                                "dependencies": [],
                            },
                            {
                                "name": "mypy_stubs",
                                "source": "myext.MypyStubsHandler",
                                "env": "stubs",
                                "dependencies": [],
                            },
                        ],
                    }
                },
            }
        }
    }

    with pytest.raises(config_models.ConfigurationError):
        resolve_interpreter_matrices(project_config)


def test_matrix_env_service_expands_to_one_service_per_interpreter() -> None:
    """A service bound to a matrix environment is replicated into one service
    entry per interpreter, with every other field preserved.

    Without this, handlers running in a matrixed env would have no service
    instance to inject in that ER, since services are per-ER singletons and
    each interpreter is a separate, independent ER.
    """
    project_config: dict[str, Any] = {
        "tool": {
            "finecode": {
                "env": {
                    "testing": {
                        "interpreters": [
                            "cpython@3.11",
                            "cpython@3.12",
                            "pypy@3.11",
                        ]
                    },
                },
                "action": {},
                "service": [
                    {
                        "interface": "pytest_runner",
                        "source": "fine_pytest.PytestService",
                        "env": "testing",
                        "dependencies": ["pytest==8.*"],
                    }
                ],
            }
        }
    }

    resolve_interpreter_matrices(project_config)

    services = project_config["tool"]["finecode"]["service"]
    assert len(services) == 3
    assert {s["env"] for s in services} == _TESTING_CONCRETE_ENV_NAMES
    for service in services:
        assert service["interface"] == "pytest_runner"
        assert service["source"] == "fine_pytest.PytestService"
        assert service["dependencies"] == ["pytest==8.*"]


def test_service_in_single_env_is_unchanged() -> None:
    """A service bound to a plain (non-matrix) env is left unchanged, even
    when another matrix environment exists in the same project.

    Guards against the service-replication step misclassifying a
    single-interpreter env as a matrix environment target.
    """
    project_config: dict[str, Any] = {
        "tool": {
            "finecode": {
                "env": {
                    "testing": {
                        "interpreters": [
                            "cpython@3.11",
                            "cpython@3.12",
                            "pypy@3.11",
                        ]
                    },
                    "dev_no_runtime": {},
                },
                "action": {},
                "service": [
                    {
                        "interface": "some_service",
                        "source": "myext.SomeService",
                        "env": "dev_no_runtime",
                        "dependencies": [],
                    }
                ],
            }
        }
    }

    resolve_interpreter_matrices(project_config)

    services = project_config["tool"]["finecode"]["service"]
    assert len(services) == 1
    assert services[0]["env"] == "dev_no_runtime"


def test_matrix_env_referenced_only_by_service_still_expands() -> None:
    """A matrix environment named only by a service (no handler at all) still
    expands into concrete children, proving expansion is driven by the env
    table, not by handler references.

    Without this, a matrix environment used purely to host a shared service
    (no action handler bound to it) would never be expanded, leaving the
    matrix environment name behind as a phantom, undiscoverable-by-design env.
    """
    project_config: dict[str, Any] = {
        "tool": {
            "finecode": {
                "env": {
                    "testing": {
                        "interpreters": [
                            "cpython@3.11",
                            "cpython@3.12",
                            "pypy@3.11",
                        ]
                    },
                },
                "action": {},
                "service": [
                    {
                        "interface": "pytest_runner",
                        "source": "fine_pytest.PytestService",
                        "env": "testing",
                        "dependencies": [],
                    }
                ],
            }
        }
    }

    resolve_interpreter_matrices(project_config)

    env_table = project_config["tool"]["finecode"]["env"]
    deps_groups = project_config["dependency-groups"]
    assert set(env_table.keys()) == _TESTING_CONCRETE_ENV_NAMES
    assert set(deps_groups.keys()) == _TESTING_CONCRETE_ENV_NAMES
    services = project_config["tool"]["finecode"]["service"]
    assert {s["env"] for s in services} == _TESTING_CONCRETE_ENV_NAMES


# ---------------------------------------------------------------------------
# read_preset_config — preset-level user config
# ---------------------------------------------------------------------------


def _write_minimal_preset(preset_dir: pathlib.Path) -> None:
    _write_toml(
        preset_dir / "preset.toml",
        '[tool.finecode.action.lint]\nsource = "myext.LintAction"\nhandlers = []\n',
    )


def test_preset_user_config_absent_is_noop(tmp_path: pathlib.Path) -> None:
    """Absence of a preset-level finecode-user.toml leaves preset_toml unchanged.

    Developers without a personal preset user file must get the standard preset
    config without any modification or error.
    """
    _write_minimal_preset(tmp_path)
    preset_toml, preset_config = read_preset_config(tmp_path / "preset.toml", "mypkg")
    assert "lint" in preset_toml["tool"]["finecode"]["action"]
    # No extra keys injected
    action = preset_toml["tool"]["finecode"]["action"]["lint"]
    assert action["source"] == "myext.LintAction"


def test_preset_user_config_merges_handler(tmp_path: pathlib.Path) -> None:
    """A preset-level finecode-user.toml adds a handler override into the preset config.

    A developer who wants a personal action in every project that includes a
    preset can declare it once in the preset directory rather than in N project
    user files.
    """
    _write_minimal_preset(tmp_path)
    _write_toml(
        tmp_path / "finecode-user.toml",
        '[action.lint.handlers.ruff]\nconfig.line_length = 120\n',
    )
    preset_toml, _ = read_preset_config(tmp_path / "preset.toml", "mypkg")

    handlers = preset_toml["tool"]["finecode"]["action"]["lint"].get("handlers", {})
    # After merging the dict-keyed shorthand, a ruff entry should be present
    # (either as a dict key or as a list item named "ruff")
    if isinstance(handlers, dict):
        assert "ruff" in handlers
        assert handlers["ruff"]["config"]["line_length"] == 120
    else:
        ruff = next((h for h in handlers if h.get("name") == "ruff"), None)
        assert ruff is not None
        assert ruff["config"]["line_length"] == 120


def test_preset_user_config_presets_appends_not_replaces(tmp_path: pathlib.Path) -> None:
    """A preset-level finecode-user.toml's `presets` list extends the preset's own
    list rather than replacing it.

    A developer adding a personal sub-preset (e.g. `fine_python_aksem`) to a
    shared preset like `finecode_dev_common_preset` must not lose that shared
    preset's own extended presets (e.g. `fine_dep_graph`) in the process —
    otherwise every action declared only by those other presets loses its
    `source` in every project that uses the shared preset.
    """
    _write_toml(
        tmp_path / "preset.toml",
        '[tool.finecode]\n'
        'presets = [{ source = "fine_dep_graph" }, { source = "fine_arch_facts" }]\n',
    )
    _write_toml(
        tmp_path / "finecode-user.toml",
        'presets = [{ source = "fine_python_aksem" }]\n',
    )
    preset_toml, preset_config = read_preset_config(tmp_path / "preset.toml", "mypkg")

    extends_sources = [p.source for p in preset_config.extends]
    assert extends_sources == ["fine_dep_graph", "fine_arch_facts", "fine_python_aksem"]


def test_preset_user_config_workspace_table_raises(tmp_path: pathlib.Path) -> None:
    """A [workspace] table in a preset-level finecode-user.toml is rejected.

    Same constraint as at the project level — workspace settings belong only in
    finecode-workspace.toml.
    """
    _write_minimal_preset(tmp_path)
    _write_toml(tmp_path / "finecode-user.toml", "[workspace]\nfoo = 1\n")
    with pytest.raises(config_models.ConfigurationError, match="workspace"):
        read_preset_config(tmp_path / "preset.toml", "mypkg")
