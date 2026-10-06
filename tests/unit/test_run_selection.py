"""Tests for `run_selection.validate_run_selectors` — the WM-side cross-project
`--interpreter` selector validator for run entry points."""

from __future__ import annotations

import pathlib
import types

import pytest

from finecode.wm_server import domain
from finecode.wm_server.services.run_service.exceptions import ActionRunFailed
from finecode.wm_server.services.run_service.run_selection import (
    check_variant_selection,
    validate_run_selectors,
)


def _matrix_env_table(base: str, versions: list[str]) -> dict[str, dict]:
    return {f"{base}@cpython-{v}": {"interpreter": f"cpython@{v}"} for v in versions}


def _raw_config(env_table: dict[str, dict]) -> dict:
    return {"tool": {"finecode": {"env": env_table}}}


def _fake_ws_context(
    raw_configs_by_path: dict[pathlib.Path, dict],
) -> types.SimpleNamespace:
    return types.SimpleNamespace(ws_projects_raw_configs=raw_configs_by_path)


class TestValidateRunSelectors:
    def test_unknown_interpreter_selector_raises(self) -> None:
        project_path = pathlib.Path("/ws/project_a")
        ws_context = _fake_ws_context(
            {project_path: _raw_config(_matrix_env_table("testing", ["3.11", "3.12"]))}
        )

        with pytest.raises(ActionRunFailed):
            validate_run_selectors(
                interpreter_selectors=["cpython@3.14"],
                project_paths=[project_path],
                ws_context=ws_context,
            )

    def test_selector_known_in_at_least_one_of_several_projects_does_not_raise(
        self,
    ) -> None:
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
            interpreter_selectors=["cpython@3.11"],
            project_paths=[project_a, project_b],
            ws_context=ws_context,
        )

    def test_empty_project_list_does_not_raise(self) -> None:
        ws_context = _fake_ws_context({})

        validate_run_selectors(
            interpreter_selectors=["cpython@3.14"],
            project_paths=[],
            ws_context=ws_context,
        )


def _variant_ws_context() -> tuple[pathlib.Path, pathlib.Path, types.SimpleNamespace]:
    from finecode.wm_server import testing as wm_testing

    path_a = pathlib.Path("/ws/project_a")
    path_b = pathlib.Path("/ws/project_b")
    project_a = wm_testing.make_multi_env_action_project(
        dir_path=path_a,
        action_name="run_tests",
        handler_envs=["testing@cpython-3.11", "testing@cpython-3.12"],
    )
    project_a.name = "project_a"
    for handler in project_a.actions[0].handlers:
        if handler.env == "testing@cpython-3.11":
            handler.interpreter = "cpython@3.11"
        elif handler.env == "testing@cpython-3.12":
            handler.interpreter = "cpython@3.12"
    project_b = wm_testing.make_multi_env_action_project(
        dir_path=path_b,
        action_name="run_tests",
        handler_envs=["testing@cpython-3.12", "testing@cpython-3.13"],
    )
    project_b.name = "project_b"
    for handler in project_b.actions[0].handlers:
        if handler.env == "testing@cpython-3.12":
            handler.interpreter = "cpython@3.12"
        elif handler.env == "testing@cpython-3.13":
            handler.interpreter = "cpython@3.13"
    ws_context = types.SimpleNamespace(
        ws_projects={path_a: project_a, path_b: project_b},
        ws_projects_raw_configs={},
    )
    return path_a, path_b, ws_context


class TestCheckVariantSelection:
    async def test_partial_empty_warns_once_naming_empty_project(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from finecode import user_messages

        path_a, path_b, ws_context = _variant_ws_context()
        warnings: list[str] = []

        async def fake_warning(message: str) -> None:
            warnings.append(message)

        monkeypatch.setattr(user_messages, "warning", fake_warning)

        await check_variant_selection(
            {path_a: ["run_tests"], path_b: ["run_tests"]},
            {path_a: {"testing@cpython-3.13"}, path_b: {"testing@cpython-3.13"}},
            ["3.13"],
            ws_context,
        )

        assert len(warnings) == 1
        assert "No interpreter variant selected" in warnings[0]
        assert "project_a" in warnings[0]

    async def test_all_empty_raises_without_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from finecode import user_messages

        path_a, path_b, ws_context = _variant_ws_context()
        warnings: list[str] = []

        async def fake_warning(message: str) -> None:
            warnings.append(message)

        monkeypatch.setattr(user_messages, "warning", fake_warning)

        with pytest.raises(ActionRunFailed):
            await check_variant_selection(
                {path_a: ["run_tests"], path_b: ["run_tests"]},
                {path_a: set(), path_b: set()},
                ["3.13"],
                ws_context,
            )

        assert warnings == []

    async def test_no_narrowing_never_warns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from finecode import user_messages

        path_a, path_b, ws_context = _variant_ws_context()
        warnings: list[str] = []

        async def fake_warning(message: str) -> None:
            warnings.append(message)

        monkeypatch.setattr(user_messages, "warning", fake_warning)

        await check_variant_selection(
            {path_a: ["run_tests"], path_b: ["run_tests"]},
            {path_a: None, path_b: None},
            [],
            ws_context,
        )

        assert warnings == []

    async def test_non_matrixed_action_with_empty_set_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from finecode import user_messages
        from finecode.wm_server import testing as wm_testing

        path = pathlib.Path("/ws/project_a")
        project = wm_testing.make_single_action_project(
            dir_path=path,
            action_name="run_tests",
            handler_env="dev_no_runtime",
        )
        ws_context = types.SimpleNamespace(
            ws_projects={path: project},
            ws_projects_raw_configs={},
        )
        warnings: list[str] = []

        async def fake_warning(message: str) -> None:
            warnings.append(message)

        monkeypatch.setattr(user_messages, "warning", fake_warning)

        await check_variant_selection(
            {path: ["run_tests"]},
            {path: set()},
            ["3.13"],
            ws_context,
        )

        assert warnings == []
