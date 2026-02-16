import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions import \
    get_src_artifact_registries as get_src_artifact_registries_action
from finecode_extension_api.actions import \
    is_artifact_published_to_registry as is_artifact_published_to_registry_action
from finecode_extension_api.interfaces import (
    iactionrunner,
    ihttpclient,
    ilogger,
    iprojectinfoprovider,
)


@dataclasses.dataclass
class IsArtifactPublishedToRegistryPyHandlerConfig(code_action.ActionHandlerConfig): ...


class IsArtifactPublishedToRegistryPyHandler(
    code_action.ActionHandler[
        is_artifact_published_to_registry_action.IsArtifactPublishedToRegistryAction,
        IsArtifactPublishedToRegistryPyHandlerConfig,
    ]
):
    def __init__(
        self,
        config: IsArtifactPublishedToRegistryPyHandlerConfig,
        action_runner: iactionrunner.IActionRunner,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        logger: ilogger.ILogger,
        http_client: ihttpclient.IHttpClient,
    ) -> None:
        self.config = config
        self.action_runner = action_runner
        self.project_info_provider = project_info_provider
        self.logger = logger
        self.http_client = http_client

    async def run(
        self,
        payload: is_artifact_published_to_registry_action.IsArtifactPublishedToRegistryRunPayload,
        run_context: is_artifact_published_to_registry_action.IsArtifactPublishedToRegistryRunContext,
    ) -> is_artifact_published_to_registry_action.IsArtifactPublishedToRegistryRunResult:
        run_meta = run_context.meta

        # Get package name from project config
        src_artifact_raw_def = await self.project_info_provider.get_project_raw_config(
            project_def_path=payload.src_artifact_def_path
        )
        package_name = src_artifact_raw_def.get("project", {}).get("name", None)

        if package_name is None:
            raise code_action.ActionFailedException(
                f"Package name not found in {payload.src_artifact_def_path}"
            )

        if not isinstance(package_name, str):
            raise code_action.ActionFailedException(
                f"project.name in {payload.src_artifact_def_path} expected to be a string, but is {type(package_name)}"
            )
        
        # normalize package name
        package_name = package_name.replace('_', '-')

        # Get registries using the action
        get_registries_action = self.action_runner.get_action_by_name(
            "get_src_artifact_registries", get_src_artifact_registries_action.GetSrcArtifactRegistriesAction
        )
        registries_payload = (
            get_src_artifact_registries_action.GetSrcArtifactRegistriesRunPayload(
                src_artifact_def_path=payload.src_artifact_def_path
            )
        )
        registries_result = await self.action_runner.run_action(
            action=get_registries_action, payload=registries_payload, meta=run_meta
        )

        # Find the registry by name
        registry_url = None
        for registry in registries_result.registries:
            if registry.name == payload.registry_name:
                registry_url = registry.url
                break

        if registry_url is None:
            raise code_action.ActionFailedException(
                f"Registry '{payload.registry_name}' not found in configuration"
            )

        # Check if package version exists using PyPI Simple API
        check_url = f"{registry_url.rstrip('/')}/simple/{package_name}/"

        self.logger.debug(
            f"Checking if {package_name} {payload.version} is published to {payload.registry_name} at {check_url}"
        )

        try:
            async with self.http_client.session() as session:
                response = await session.get(check_url, headers={"Accept": "application/vnd.pypi.simple.v1+json"}, timeout=10.0)
        except Exception as exception:
            raise code_action.ActionFailedException(
                f"Error checking publication status: {str(exception)}"
            ) from exception

        if response.status_code == 404:
            # Package does not exist in the registry yet
            is_published_by_dist_path = {dist_path: False for dist_path in payload.dist_artifact_paths}
            return is_artifact_published_to_registry_action.IsArtifactPublishedToRegistryRunResult(
                is_published_by_dist_path=is_published_by_dist_path
            )

        response_json = response.json()
        version_list = response_json.get('versions', None)
        if version_list is None:
            raise code_action.ActionFailedException("No 'versions' key in response from registry")
        
        if not isinstance(version_list, list):
            raise code_action.ActionFailedException("'versions' key in response from registry expected to be a list")
        
        version_is_published = payload.version in version_list
        dist_artifact_paths = payload.dist_artifact_paths
        if version_is_published:
            try:
                published_files_objs = response_json['files']
            except KeyError as exception:
                raise code_action.ActionFailedException("'files' key is missing in response from registry") from exception

            if not isinstance(published_files_objs, list):
                raise code_action.ActionFailedException("'files' key in response from registry expected to be a list")

            try:
                published_file_names = [file_obj['filename'] for file_obj in published_files_objs]
            except KeyError as exception:
                raise code_action.ActionFailedException("File object has no 'filename' key") from exception

            is_published_by_dist_path = {dist_path: dist_path.name in published_file_names for dist_path in dist_artifact_paths}
        else:
            is_published_by_dist_path = {dist_path: False for dist_path in dist_artifact_paths}

        return is_artifact_published_to_registry_action.IsArtifactPublishedToRegistryRunResult(
            is_published_by_dist_path=is_published_by_dist_path
        )
