import dataclasses
import pathlib

from finecode_extension_api import code_action
from finecode_extension_api.actions.observability.discover_wal_sources_action import (
    DiscoverWalSourcesAction,
    DiscoverWalSourcesRunPayload,
    DiscoverWalSourcesRunResult,
)
from finecode_extension_api.actions.observability.ingest_wal_to_store_action import WalSourceSpec
from finecode_extension_api.interfaces import (
    iextensionrunnerinfoprovider,
    ilogger,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import path_to_resource_uri


_WAL_WRITER_DIRS: tuple[tuple[str, str], ...] = (
    ("wm", "wm"),
    ("er", "er"),
)


@dataclasses.dataclass
class DiscoverWalSourcesActionHandlerConfig(code_action.ActionHandlerConfig): ...


class DiscoverWalSourcesActionHandler(
    code_action.ActionHandler[
        DiscoverWalSourcesAction,
        DiscoverWalSourcesActionHandlerConfig,
    ]
):
    """Discover WAL sources as a standalone action result."""

    def __init__(
        self,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        runner_info_provider: iextensionrunnerinfoprovider.IExtensionRunnerInfoProvider,
        logger: ilogger.ILogger,
    ) -> None:
        self.project_info_provider = project_info_provider
        self.runner_info_provider = runner_info_provider
        self.logger = logger

    async def run(
        self,
        payload: DiscoverWalSourcesRunPayload,
        run_context: code_action.RunActionContext[DiscoverWalSourcesRunPayload],
    ) -> DiscoverWalSourcesRunResult:
        project_raw_config = await self.project_info_provider.get_current_project_raw_config()
        env_names = list(project_raw_config.get("dependency-groups", {}).keys())

        source_specs: list[WalSourceSpec] = []
        for env_name in env_names:
            venv_dir: pathlib.Path = self.runner_info_provider.get_venv_dir_path_of_env(
                env_name
            )
            for writer_suffix, writer_dir in _WAL_WRITER_DIRS:
                wal_dir = venv_dir / "state" / "finecode" / "wal" / writer_dir
                if wal_dir.is_dir():
                    source_id = f"{env_name}_{writer_suffix}"
                    source_specs.append(
                        WalSourceSpec(
                            source_id=source_id,
                            format="jsonl_events",
                            location_uri=path_to_resource_uri(wal_dir),
                        )
                    )

        self.logger.debug(f"Discovered WAL sources: {[s.source_id for s in source_specs]}")

        return DiscoverWalSourcesRunResult(source_specs=source_specs)
