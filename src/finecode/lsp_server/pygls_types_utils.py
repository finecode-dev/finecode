from pathlib import Path


def uri_str_to_path(uri_str: str) -> Path:
    return Path(uri_str.replace("file://", ""))


def path_to_uri_str(path: Path) -> str:
    return f"file://{path.as_posix()}"


__all__ = ["uri_str_to_path", "path_to_uri_str"]
