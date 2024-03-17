import importlib
import os
import re
import site
from pathlib import Path

from command_runner import command_runner
from loguru import logger


class VenvNotFound(Exception):
    ...


def get_project_venv_path(project_path: Path) -> Path:
    old_current_dir = os.getcwd()
    os.chdir(project_path)
    exit_code, output = command_runner(f"poetry env info")
    os.chdir(old_current_dir)
    if exit_code != 0:
        logger.error(f"Cannot get env info in project {project_path}")
        raise VenvNotFound()
    venv_path_match = re.search("Path:\ *(?P<venv_path>.*)\n", output)
    if venv_path_match is None:
        logger.error(f"Venv path not found in poetry output")
        raise VenvNotFound()

    project_venv_path_str = venv_path_match.group("venv_path")
    if project_venv_path_str == "NA":
        # it can be checked whether venv exists with `poetry env list` and then either automatically
        # activated or suggested to user to activate
        logger.error(
            f"No virtualenv found in {project_path}. Maybe it is not activated?"
        )
        raise VenvNotFound()

    return Path(project_venv_path_str)


def get_current_venv_path() -> Path:
    return Path(site.getsitepackages()[0]).parent.parent.parent


def run_cmd_in_dir(cmd: str, dir_path: Path) -> tuple[int, str]:
    old_current_dir = os.getcwd()
    os.chdir(dir_path)
    exit_code, output = command_runner(cmd)
    os.chdir(old_current_dir)
    return (exit_code, output)


def import_class_by_source_str(source: str):
    cls_name = source.split(".")[-1]
    module_path = ".".join(source.split(".")[:-1])

    # TODO: handle errors
    module = importlib.import_module(module_path)
    try:
        cls = module.__dict__[cls_name]
        return cls
    except KeyError:
        logger.error(f"Class {cls_name} not found in module {module_path}")
        raise ModuleNotFoundError()
