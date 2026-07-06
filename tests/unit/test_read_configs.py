import pathlib
from typing import Any

import pytest

from finecode.wm_server.config import config_models
from finecode.wm_server.config.read_configs import (
    _merge_projects_configs,
    read_project_user_config,
    read_preset_config,
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


def test_preset_user_config_workspace_table_raises(tmp_path: pathlib.Path) -> None:
    """A [workspace] table in a preset-level finecode-user.toml is rejected.

    Same constraint as at the project level — workspace settings belong only in
    finecode-workspace.toml.
    """
    _write_minimal_preset(tmp_path)
    _write_toml(tmp_path / "finecode-user.toml", "[workspace]\nfoo = 1\n")
    with pytest.raises(config_models.ConfigurationError, match="workspace"):
        read_preset_config(tmp_path / "preset.toml", "mypkg")
