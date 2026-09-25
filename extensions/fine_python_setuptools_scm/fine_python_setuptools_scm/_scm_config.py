import pathlib

from finecode_extension_api.interfaces import ilogger, iprojectinfoprovider
from finecode_extension_api.resource_uri import ResourceUri, resource_uri_to_path
from setuptools_scm import Configuration


def resolve_def_path(
    payload_def_path: ResourceUri | None,
    project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
) -> pathlib.Path:
    if payload_def_path is not None:
        return resource_uri_to_path(payload_def_path)
    return project_info_provider.get_current_project_def_path()


def load_configuration(
    def_path: pathlib.Path, logger: ilogger.ILogger
) -> Configuration:
    pyproject = def_path.as_posix()
    try:
        # could be optimized by providing config from project_info_provider instead
        # of reading file each time
        config = Configuration.from_file(pyproject, root=None)
    except (LookupError, FileNotFoundError) as ex:
        # no pyproject.toml OR no [tool.setuptools_scm]
        logger.warning(
            f"Warning: could not use {pyproject},"
            " using default configuration.\n"
            f" Reason: {ex}."
        )
        config = Configuration(root=def_path.parent.as_posix())
    return config


def version_file_path(
    config: Configuration, logger: ilogger.ILogger
) -> pathlib.Path | None:
    if getattr(config, "write_to", None):
        logger.debug(
            "get_src_artifact_version_setuptools_scm_format: ignoring legacy"
            " [tool.setuptools_scm] write_to; only version_file is formatted"
        )
    if not config.version_file or config.relative_to is None:
        return None
    return pathlib.Path(config.relative_to).parent / config.version_file
