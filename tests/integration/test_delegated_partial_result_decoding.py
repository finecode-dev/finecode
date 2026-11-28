"""Real-process decoding of a delegated child's partials (issue #914).

A parent iterating ``run_action_iter`` must receive each partial as the child's
typed result, whether the child streams partials or returns one final result,
and whether it runs in the parent's env or in another one.
"""

from __future__ import annotations

import pathlib
import shutil
import sys
import sysconfig
import typing

import pytest

from finecode.wm_server import domain
from finecode.wm_server.services import shutdown_service

FIXTURE_MODULE = "delegated_partial_decoding_probe"
PARENT_ENV = "parent"


def _repo_fixture_module() -> pathlib.Path:
    return pathlib.Path(__file__).parent / "fixtures" / f"{FIXTURE_MODULE}.py"


def _repair_copied_venv_path(
    venv_dir: pathlib.Path, old_venv_dir: pathlib.Path
) -> None:
    if sys.platform == "win32":
        activate_path = venv_dir / "Scripts" / "activate.bat"
    else:
        activate_path = venv_dir / "bin" / "activate"
    content = activate_path.read_text(encoding="utf-8")
    activate_path.write_text(
        content.replace(str(old_venv_dir), str(venv_dir)), encoding="utf-8"
    )


def _copy_env(src: pathlib.Path, dst: pathlib.Path) -> None:
    def _ignore(_dir: str, names: list[str]) -> list[str]:
        return [n for n in names if n == "__pycache__" or n.endswith(".pyc")]

    shutil.copytree(src, dst, symlinks=True, ignore=_ignore)


def _prepare_project(tmp_path: pathlib.Path, leaf_env: str) -> pathlib.Path:
    for mod in ("finecode", "finecode_extension_runner", "finecode_extension_api"):
        try:
            __import__(mod)
        except ImportError as exc:
            raise RuntimeError(
                f"fixture setup failed: required module {mod!r} not importable: {exc}"
            ) from exc
    source_env = pathlib.Path(sys.executable).parent.parent
    project_dir = tmp_path / "project"
    fixture_modules = tmp_path / "fixture_modules"
    project_dir.mkdir(parents=True, exist_ok=True)
    fixture_modules.mkdir(parents=True, exist_ok=True)
    shutil.copy(_repo_fixture_module(), fixture_modules / f"{FIXTURE_MODULE}.py")
    venvs_dir = project_dir / ".venvs"
    venvs_dir.mkdir(exist_ok=True)
    env_names = ["dev_workspace", PARENT_ENV]
    if leaf_env != PARENT_ENV:
        env_names.append(leaf_env)
    for env_name in env_names:
        _copy_env(source_env, venvs_dir / env_name)
        _repair_copied_venv_path(venvs_dir / env_name, source_env)
    purelib_relative = pathlib.Path(sysconfig.get_paths()["purelib"]).relative_to(
        source_env
    )
    for env_name in env_names:
        purelib = venvs_dir / env_name / purelib_relative
        purelib.mkdir(parents=True, exist_ok=True)
        (purelib / f"{FIXTURE_MODULE}.pth").write_text(
            f"{fixture_modules}\n", encoding="utf-8"
        )
    leaf_groups = (
        f'{leaf_env} = ["finecode_extension_runner"]\n'
        if leaf_env != PARENT_ENV
        else ""
    )
    leaf_actions = "".join(
        f"""
[tool.finecode.action.probe_{name}_leaf]
source = "{FIXTURE_MODULE}.{cls}"
handlers = [{{ name = "{name}_leaf", source = "{FIXTURE_MODULE}.{cls.removesuffix("Action")}Handler", env = "{leaf_env}" }}]
"""
        for name, cls in (
            ("streaming", "StreamingLeafAction"),
            ("required", "RequiredFieldLeafAction"),
            ("final", "FinalLeafAction"),
        )
    )
    project_dir.joinpath("pyproject.toml").write_text(
        f"""[project]
name = "delegated-partial-decoding-probe"
version = "0.0.0"

[dependency-groups]
dev_workspace = ["finecode"]
{PARENT_ENV} = ["finecode_extension_runner"]
{leaf_groups}
[tool.finecode.action.probe_parent]
source = "{FIXTURE_MODULE}.ProbeParentAction"
handlers = [{{ name = "parent", source = "{FIXTURE_MODULE}.ProbeParentHandler", env = "{PARENT_ENV}" }}]
{leaf_actions}""",
        encoding="utf-8",
    )
    return project_dir


async def _run_parent(
    wm_client: typing.Any, project_dir: pathlib.Path, leaf: str
) -> dict | str:
    try:
        result = await wm_client.request(
            "actions/run",
            {
                "actionSource": f"{FIXTURE_MODULE}.ProbeParentAction",
                "project": str(project_dir),
                "params": {"leaf": leaf},
                "options": {
                    "resultFormats": ["json"],
                    "trigger": "user",
                    "devEnv": "ci",
                },
            },
            timeout=60.0,
        )
    except RuntimeError as exc:
        return f"failed: {exc}"
    if result["returnCode"] != 0:
        return f"returnCode={result['returnCode']}: {result}"
    parent_json = result["resultByFormat"]["json"]
    return {
        "items": parent_json["items"],
        "result_types": sorted(set(parent_json["result_types"])),
    }


@pytest.mark.parametrize("leaf_env", [PARENT_ENV, "leaf"])
async def test_parent_receives_typed_partials_from_delegated_child(
    wm_client: typing.Any, tmp_path: pathlib.Path, leaf_env: str
) -> None:
    """Streamed, required-field and final-result children all decode to their type."""
    project_dir = _prepare_project(tmp_path, leaf_env)
    ctx = wm_client.ws_context
    ctx.wal_writer = None
    try:
        await wm_client.request(
            "client/initialize", {"clientId": "partial-decoding"}, timeout=60.0
        )
        add_result = await wm_client.request(
            "workspace/addDir", {"dirPath": str(project_dir)}, timeout=60.0
        )
        assert add_result is not None
        project = ctx.ws_projects.get(project_dir)
        assert project is not None
        assert project.status == domain.ProjectStatus.CONFIG_VALID

        outcomes = {
            leaf: await _run_parent(wm_client, project_dir, leaf)
            for leaf in ("streaming", "required", "final")
        }

        assert outcomes == {
            "streaming": {
                "items": ["streaming#0", "streaming#1", "streaming#2"],
                "result_types": ["TypedResult"],
            },
            "required": {
                "items": ["required#0", "required#1", "required#2"],
                "result_types": ["RequiredFieldResult"],
            },
            "final": {"items": ["final#0"], "result_types": ["TypedResult"]},
        }
    finally:
        await shutdown_service.on_shutdown(ctx)
