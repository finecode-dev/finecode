import dataclasses

from fine_dist_artifacts import list_published_artifacts_action
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    idataclasscodec,
    ihttpclient,
    ilogger,
    iprojectinfoprovider,
    irepositorycredentialsprovider,
)
from finecode_extension_api.resource_uri import resource_uri_to_path
from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version

from fine_python_package_info import registry_endpoints


@dataclasses.dataclass
class _ListingFile:
    filename: str


@dataclasses.dataclass
class _Listing:
    files: list[_ListingFile]


def _listing_filenames(
    body: object,
    *,
    codec: idataclasscodec.IDataclassCodec,
    registry_name: str,
    check_url: str,
) -> list[str]:
    """Validate the whole listing body and collect filename strings.

    Unsupported names are omitted later; retrying such a payload can offer
    a duplicate and fail.
    """
    try:
        listing = codec.structure(
            body, _Listing, unknown_fields=idataclasscodec.UnknownFields.IGNORE
        )
    except idataclasscodec.StructureError as error:
        raise code_action.ActionFailedException(
            f"Registry '{registry_name}' returned malformed listing at"
            f" {check_url}: {error.message}"
        ) from error
    return [file.filename for file in listing.files]


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
        codec: idataclasscodec.IDataclassCodec,
    ) -> None:
        self.config = config
        self.project_info_provider = project_info_provider
        self.repository_credentials_provider = repository_credentials_provider
        self.logger = logger
        self.http_client = http_client
        self.codec = codec

    async def run(
        self,
        payload: list_published_artifacts_action.ListPublishedArtifactsRunPayload,
        run_context: list_published_artifacts_action.ListPublishedArtifactsRunContext,
    ) -> list_published_artifacts_action.ListPublishedArtifactsRunResult:
        src_artifact_raw_def = await self.project_info_provider.get_project_raw_config(
            project_def_path=resource_uri_to_path(payload.src_artifact_def_path)
        )
        package_name = src_artifact_raw_def.get("project", {}).get("name", None)
        if package_name is None:
            raise code_action.ActionFailedException(
                f"project.name not found in config at {payload.src_artifact_def_path}"
            )

        try:
            requested_version = Version(payload.version)
        except InvalidVersion as exception:
            raise code_action.ActionFailedException(
                f"Invalid version {payload.version!r} for registry"
                f" '{payload.registry_name}': {exception}"
            ) from exception

        canonical_package_name = canonicalize_name(package_name)

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
        check_url = f"{repository.index_url.rstrip('/')}/{canonical_package_name}/"

        self.logger.debug(
            f"Checking published files for {canonical_package_name} {payload.version} at {check_url}"
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

        if not 200 <= response.status_code < 300:
            raise code_action.ActionFailedException(
                f"Registry '{payload.registry_name}' returned status"
                f" {response.status_code} for {check_url}"
            )

        try:
            response_json = response.json()
        except Exception as exception:
            raise code_action.ActionFailedException(
                f"Registry '{payload.registry_name}' returned malformed listing at"
                f" {check_url} (status {response.status_code}): {exception}"
            ) from exception

        filenames = _listing_filenames(
            response_json,
            codec=self.codec,
            registry_name=payload.registry_name,
            check_url=check_url,
        )

        matched: list[str] = []
        for filename in filenames:
            try:
                parsed_name, parsed_version, *_ = parse_wheel_filename(filename)
            except InvalidWheelFilename:
                try:
                    parsed_name, parsed_version = parse_sdist_filename(filename)
                except InvalidSdistFilename:
                    continue
            if (
                canonicalize_name(str(parsed_name)) == canonical_package_name
                and parsed_version == requested_version
            ):
                matched.append(filename)

        return list_published_artifacts_action.ListPublishedArtifactsRunResult(
            filenames=matched
        )
