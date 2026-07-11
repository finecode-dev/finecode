"""Tests for `run_selection.validate_run_selectors` — the WM-side cross-project
`--env`/`--interpreter` selector validator for run entry points (mirrors
`prepare_envs_service`'s cross-project validation, but tolerates a selector
that matches at least one in-scope project rather than requiring all of
them)."""
from __future__ import annotations

import pathlib
import types

import pytest

from finecode.wm_server.services.run_service.exceptions import ActionRunFailed
from finecode.wm_server.services.run_service.run_selection import validate_run_selectors


def _matrix_env_table(base: str, versions: list[str]) -> dict[str, dict]:
    return {
        f"{base}@cpython-{v}": {"interpreter": f"cpython@{v}"} for v in versions
    }


def _raw_config(env_table: dict[str, dict]) -> dict:
    return {"tool": {"finecode": {"env": env_table}}}


def _fake_ws_context(raw_configs_by_path: dict[pathlib.Path, dict]) -> types.SimpleNamespace:
    return types.SimpleNamespace(ws_projects_raw_configs=raw_configs_by_path)


class TestValidateRunSelectors:
    def test_unknown_interpreter_selector_raises(self) -> None:
        project_path = pathlib.Path("/ws/project_a")
        ws_context = _fake_ws_context(
            {project_path: _raw_config(_matrix_env_table("testing", ["3.11", "3.12"]))}
        )

        with pytest.raises(ActionRunFailed):
            validate_run_selectors(
                env_selectors=[],
                interpreter_selectors=["cpython@3.14"],
                project_paths=[project_path],
                ws_context=ws_context,
            )

    def test_unknown_env_selector_raises(self) -> None:
        project_path = pathlib.Path("/ws/project_a")
        ws_context = _fake_ws_context(
            {project_path: _raw_config(_matrix_env_table("testing", ["3.11", "3.12"]))}
        )

        with pytest.raises(ActionRunFailed):
            validate_run_selectors(
                env_selectors=["nonexistent"],
                interpreter_selectors=[],
                project_paths=[project_path],
                ws_context=ws_context,
            )

    def test_selector_known_in_at_least_one_of_several_projects_does_not_raise(self) -> None:
        """A selector valid for one project but absent in a sibling project
        must not fail the whole run (multi-project nuance)."""
        project_a = pathlib.Path("/ws/project_a")
        project_b = pathlib.Path("/ws/project_b")
        ws_context = _fake_ws_context(
            {
                project_a: _raw_config(_matrix_env_table("testing", ["3.11", "3.12"])),
                project_b: _raw_config({"docs": {}}),
            }
        )

        validate_run_selectors(
            env_selectors=["testing"],
            interpreter_selectors=["cpython@3.11"],
            project_paths=[project_a, project_b],
            ws_context=ws_context,
        )

    def test_empty_project_list_does_not_raise(self) -> None:
        ws_context = _fake_ws_context({})

        validate_run_selectors(
            env_selectors=["nonexistent"],
            interpreter_selectors=["cpython@3.14"],
            project_paths=[],
            ws_context=ws_context,
        )
