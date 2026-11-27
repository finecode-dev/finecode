import pathlib
import sys


def get_uv_executable() -> pathlib.Path:
    scripts_dir = pathlib.Path(sys.executable).parent
    if sys.platform == "win32":
        return scripts_dir / "uv.exe"
    return scripts_dir / "uv"
