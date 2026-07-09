"""Shared helpers for observability log handlers."""

import pathlib

from finecode_extension_api.interfaces import iextensionrunnerinfoprovider

_ER_PREFIX = "er:"
_DEV_WORKSPACE_ENV = "dev_workspace"
_ER_LOG_SUBDIR = "runner"


def resolve_log_dir(
    service_id: str,
    runner_info_provider: iextensionrunnerinfoprovider.IExtensionRunnerInfoProvider,
) -> pathlib.Path:
    """Resolve a service_id to its log directory.

    service_id format: '{project_name}/{local_id}'.
    ER services use local_id 'er:{env_name}' and their logs live in
    '{env_venv}/logs/runner/'. All other local IDs are WM-side services
    whose logs live in '{dev_workspace_venv}/logs/{local_id}/'.
    """
    local_id = service_id.split("/", 1)[-1]
    if local_id.startswith(_ER_PREFIX):
        env_name = local_id[len(_ER_PREFIX):]
        return (
            runner_info_provider.get_venv_dir_path_of_env(env_name)
            / "logs"
            / _ER_LOG_SUBDIR
        )
    return (
        runner_info_provider.get_venv_dir_path_of_env(_DEV_WORKSPACE_ENV)
        / "logs"
        / local_id
    )
