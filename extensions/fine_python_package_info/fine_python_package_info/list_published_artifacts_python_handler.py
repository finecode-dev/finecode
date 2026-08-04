import dataclasses

from fine_dist_artifacts import list_published_artifacts_action
from fine_python_package_info import registry_endpoints
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    ihttpclient,
    ilogger,
    iprojectinfoprovider,
    irepositorycredentialsprovider,
)


@dataclasses.dataclass
class ListPublishedArtifactsPythonHandlerConfig(code_action.ActionHandlerConfig): ...


class ListPublishedArtifactsPythonHandler(
    code_action.ActionHandler[
        list_published_artifacts_action.ListPublishedArtifactsAction,
        ListPublishedArtifactsPythonHandlerConfig,
    ]
):
    def __init__(
        self,
        config: ListPublishedArtifactsPythonHandlerConfig,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        repository_credentials_provider: irepositorycredentialsprovider.IRepositoryCredentialsProvider,
        logger: ilogger.ILogger,
        http_client: ihttpclient.IHttpClient,
    ) -> None:
        self.config = config
        self.project_info_provider = project_info_provider
        self.repository_credentials_provider = repository_credentials_provider
        self.logger = logger
        self.http_client = http_client

    async def run(
        self,
        payload: list_published_artifacts_action.ListPublishedArtifactsRunPayload,
        run_context: list_published_artifacts_action.ListPublishedArtifactsRunContext,
    ) -> list_published_artifacts_action.ListPublishedArtifactsRunResult:
        src_artifact_raw_def = await self.project_info_provider.get_project_raw_config(
            project_def_path=payload.src_artifact_def_path
        )
        package_name = src_artifact_raw_def.get("project", {}).get("name", None)
        if package_name is None:
            raise code_action.ActionFailedException(
                f"project.name not found in config at {payload.src_artifact_def_path}"
            )

        package_name = package_name.replace("_", "-")

        repository = self.repository_credentials_provider.get_repository(
            payload.registry_name
        )
        if repository is None:
            raise code_action.ActionFailedException(
                f"Registry '{payload.registry_name}' not found in configuration"
            )

        index_url_problem = registry_endpoints.index_url_problem(
            payload.registry_name, repository.index_url
        )
        if index_url_problem is not None:
            raise code_action.ActionFailedException(index_url_problem)

        # index_url is a prefix: the package being looked up is appended to it.
        check_url = f"{repository.index_url.rstrip('/')}/{package_name}/"

        self.logger.debug(
            f"Checking published files for {package_name} {payload.version} at {check_url}"
        )

        try:
            async with self.http_client.session() as session:
                response = await session.get(
                    check_url,
                    headers={"Accept": "application/vnd.pypi.simple.v1+json"},
                    timeout=10.0,
                )
        except Exception as exception:
            raise code_action.ActionFailedException(
                f"Error checking publication status: {exception}"
            ) from exception

        if response.status_code == 404:
            return list_published_artifacts_action.ListPublishedArtifactsRunResult(
                filenames=[]
            )

        response_json = response.json()
        versions = response_json.get("versions", None)
        if versions is None:
            raise code_action.ActionFailedException(
                "No 'versions' key in response from registry"
            )
        if not isinstance(versions, list):
            raise code_action.ActionFailedException(
                "'versions' key in response from registry expected to be a list"
            )

        if payload.version not in versions:
            return list_published_artifacts_action.ListPublishedArtifactsRunResult(
                filenames=[]
            )

        try:
            files = response_json["files"]
        except KeyError as exception:
            raise code_action.ActionFailedException(
                "'files' key is missing in response from registry"
            ) from exception
        if not isinstance(files, list):
            raise code_action.ActionFailedException(
                "'files' key in response from registry expected to be a list"
            )

        try:
            filenames = [file_obj["filename"] for file_obj in files]
        except KeyError as exception:
            raise code_action.ActionFailedException(
                "File object has no 'filename' key"
            ) from exception

        return list_published_artifacts_action.ListPublishedArtifactsRunResult(
            filenames=filenames
        )
