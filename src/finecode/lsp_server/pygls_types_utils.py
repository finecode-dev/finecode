from pathlib import Path


def uri_str_to_path(uri_str: str) -> Path:
    return Path(uri_str.replace("file://", ""))


__all__ = ["uri_str_to_path"]
