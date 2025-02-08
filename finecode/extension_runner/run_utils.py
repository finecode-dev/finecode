import importlib
import os
import re
import site
from pathlib import Path

from command_runner import command_runner
from loguru import logger


class VenvNotFound(Exception): ...


def get_project_venv_path(project_path: Path) -> Path:
    exit_code, output = run_cmd_in_dir("poetry env info", project_path)
    if exit_code != 0:
        logger.error(f"Cannot get env info in project {project_path}")
        raise VenvNotFound()
    venv_path_match = re.search(r"Path:\ *(?P<venv_path>.*)\n", output)
    if venv_path_match is None:
        logger.error(f"Venv path not found in poetry output")
        raise VenvNotFound()

    project_venv_path_str = venv_path_match.group("venv_path")
    if project_venv_path_str == "NA":
        # it can be checked whether venv exists with `poetry env list` and then either automatically
        # activated or suggested to user to activate
        logger.error(
            f"No virtualenv found in {project_path}. Maybe it is not activated or you have wrong default env version?"
        )
        raise VenvNotFound()

    return Path(project_venv_path_str)


def get_current_venv_path() -> Path:
    return Path(site.getsitepackages()[0]).parent.parent.parent


def run_cmd_in_dir(cmd: str, dir_path: Path) -> tuple[int, str]:
    old_current_dir = os.getcwd()
    os.chdir(dir_path)
    # remove 'VIRTUAL_ENV' env variable to avoid impact of current venv if one is activated
    old_virtual_env_value: str | None = None
    if "VIRTUAL_ENV" in os.environ:
        old_virtual_env_value = os.environ["VIRTUAL_ENV"]
        del os.environ["VIRTUAL_ENV"]
    exit_code, output = command_runner(cmd)
    os.chdir(old_current_dir)
    if old_virtual_env_value is not None:
        os.environ["VIRTUAL_ENV"] = old_virtual_env_value
    return (exit_code, output)


def import_module_member_by_source_str(source: str):
    member_name = source.split(".")[-1]
    module_path = ".".join(source.split(".")[:-1])

    # TODO: handle errors
    module = importlib.import_module(module_path)
    try:
        member = module.__dict__[member_name]
        return member
    except KeyError:
        logger.error(f"Member {member_name} not found in module {module_path}")
        raise ModuleNotFoundError()
