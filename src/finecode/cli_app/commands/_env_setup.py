"""Shared helper for create_envs + install_envs used by bootstrap and prepare-envs."""
from finecode.wm_client import ApiClient, ApiError


class EnvSetupFailed(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


async def create_and_install_envs(
    client: ApiClient,
    project_path: str,
    envs: list[dict],
    dev_env: str = "cli",
) -> None:
    """Run ``create_envs`` then ``install_envs`` for the given environments.

    Raises :class:`EnvSetupFailed` on any failure so callers can re-raise
    with their own exception type.
    """
    options = {
        "resultFormats": ["string"],
        "trigger": "user",
        "devEnv": dev_env,
    }

    try:
        create_result = await client.run_action(
            action_source="finecode_extension_api.actions.CreateEnvsAction",
            project=project_path,
            params={"envs": envs},
            options=options,
        )
    except ApiError as exc:
        raise EnvSetupFailed(f"'create_envs' failed: {exc}") from exc
    if create_result.get("returnCode", 0) != 0:
        output = (create_result.get("resultByFormat") or {}).get("string", "")
        raise EnvSetupFailed(
            f"'create_envs' failed with return code "
            f"{create_result['returnCode']}: {output}"
        )

    try:
        install_result = await client.run_action(
            action_source="finecode_extension_api.actions.InstallEnvsAction",
            project=project_path,
            params={"envs": envs},
            options=options,
        )
    except ApiError as exc:
        raise EnvSetupFailed(f"'install_envs' failed: {exc}") from exc
    if install_result.get("returnCode", 0) != 0:
        output = (install_result.get("resultByFormat") or {}).get("string", "")
        raise EnvSetupFailed(
            f"'install_envs' failed with return code "
            f"{install_result['returnCode']}: {output}"
        )
