import tomllib

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectinfoprovider

from fine_dep_graph.collect_project_dependency_info_action import (
    CollectProjectDependencyInfoAction,
    CollectProjectDependencyInfoRunContext,
    CollectProjectDependencyInfoRunPayload,
    CollectProjectDependencyInfoRunResult,
)


class FineCodePresetsInfoHandler(
    code_action.ActionHandler[
        CollectProjectDependencyInfoAction,
        code_action.ActionHandlerConfig,
    ]
):
    def __init__(
        self,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
    ) -> None:
        self.project_info_provider = project_info_provider

    async def run(
        self,
        payload: CollectProjectDependencyInfoRunPayload,
        run_context: CollectProjectDependencyInfoRunContext,
    ) -> CollectProjectDependencyInfoRunResult:
        project_dir = self.project_info_provider.get_current_project_dir_path()
        pyproject_path = project_dir / "pyproject.toml"

        if not pyproject_path.exists():
            return CollectProjectDependencyInfoRunResult()

        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)

        presets = data.get("tool", {}).get("finecode", {}).get("presets", [])
        used_preset_sources = [p["source"] for p in presets if "source" in p]

        return CollectProjectDependencyInfoRunResult(
            used_preset_sources=used_preset_sources,
        )
