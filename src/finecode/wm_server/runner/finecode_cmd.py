import re
import sys
from pathlib import Path

# Matches the standard `python -m venv` / virtualenv activate script:
#   VIRTUAL_ENV='/abs/path/.venvs/dev'
_VIRTUAL_ENV_POSIX_RE = re.compile(r"^VIRTUAL_ENV=(['\"]?)(?P<path>.*?)\1\s*$", re.MULTILINE)

# activate.bat records the same path, but some generators (e.g. uv) route it through
# a `for %%i in ("...") do @set "VIRTUAL_ENV=%%~fi"` indirection instead of a plain
# `set`, so the literal path lives inside the `for ... in (...)` clause.
_VIRTUAL_ENV_WIN_RE = re.compile(
    r'^\s*(?:@?for\s+%%\w+\s+in\s+\(|@?set\s+)"(?P<path>[^"]+)"', re.MULTILINE | re.IGNORECASE
)


def get_venv_dir_path(project_path: Path, env_name: str) -> Path:
    venv_dir_path = project_path / ".venvs" / env_name
    return venv_dir_path


class VenvRelocatedError(ValueError):
    """Raised when a venv's recorded creation path no longer matches its current
    location — its console scripts are stale and it must be recreated, not just
    reinstalled into (see ``_recorded_venv_path``)."""


def _recorded_venv_path(venv_dir_path: Path) -> Path | None:
    """Return the absolute venv path recorded in its own activate script.

    Every venv (venv/virtualenv/uv) writes the venv's absolute path into its
    activate script once, at creation time, and never touches it again. pip/uv
    also bake that same creation-time path into the shebang of every generated
    console script (``pytest``, etc.). Comparing the recorded path to
    *venv_dir_path*'s current location detects a venv whose project directory
    was later moved or renamed on disk: the venv's own ``python`` binary keeps
    working (it's a symlink to the system interpreter, unaffected by the move),
    but every console script breaks with "not found", since their shebang still
    points at a path that no longer exists.
    """
    if sys.platform == "win32":
        activate_path = venv_dir_path / "Scripts" / "activate.bat"
        pattern = _VIRTUAL_ENV_WIN_RE
    else:
        activate_path = venv_dir_path / "bin" / "activate"
        pattern = _VIRTUAL_ENV_POSIX_RE

    try:
        content = activate_path.read_text()
    except OSError:
        return None

    match = pattern.search(content)
    if match is None:
        return None

    return Path(match.group("path"))


def get_python_cmd(project_path: Path, env_name: str) -> str:
    venv_dir_path = get_venv_dir_path(project_path=project_path, env_name=env_name)

    if sys.platform == "win32":
        venv_python_path = venv_dir_path / "Scripts" / "python.exe"
    else:
        venv_python_path = venv_dir_path / "bin" / "python"

    if not venv_python_path.exists():
        # `Path.exists` returns False for invalid symlinks
        if venv_python_path.is_symlink():
            raise ValueError(
                f"Execution environment '{env_name}' is broken in project {project_path} "
                f"(python symlink cannot be resolved)"
            )

        raise ValueError(
            f"Execution environment '{env_name}' not found in project {project_path}"
        )

    recorded_venv_path = _recorded_venv_path(venv_dir_path)
    if recorded_venv_path is not None and recorded_venv_path != venv_dir_path:
        raise VenvRelocatedError(
            f"Execution environment '{env_name}' in project {project_path} was relocated "
            f"(created at '{recorded_venv_path}', now expected at '{venv_dir_path}'). "
            f"Console scripts installed in it have stale shebangs pointing at the old "
            f"path and will fail with 'not found'. Recreate the environment."
        )

    return venv_python_path.as_posix()
