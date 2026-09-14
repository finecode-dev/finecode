import pathlib
import re
import tomllib

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectinfoprovider

from fine_dep_graph.collect_project_dependency_info_action import (
    CollectProjectDependencyInfoAction,
    CollectProjectDependencyInfoRunContext,
    CollectProjectDependencyInfoRunPayload,
    CollectProjectDependencyInfoRunResult,
)

_DEP_NAME_RE = re.compile(r"^([A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?)")


def _extract_package_name(dep_spec: str) -> str:
    """Strip version specifiers and extras, returning only the package name."""
    dep_spec = dep_spec.split(";")[0].strip()
    match = _DEP_NAME_RE.match(dep_spec)
    return match.group(1) if match else dep_spec


def _infer_package_kind(project_path: pathlib.Path) -> str:
    """Infer 'core' | 'extension' | 'preset' from the project path in the workspace."""
    lower_parts = [p.lower() for p in project_path.parts]
    if "extensions" in lower_parts:
        return "extension"
    if "presets" in lower_parts:
        return "preset"
    return "core"


class PyprojectPackageInfoHandler(
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

        package_name: str = data.get("project", {}).get("name", "")
        package_kind: str = _infer_package_kind(project_dir)
        raw_deps: list[str] = data.get("project", {}).get("dependencies", [])
        pyproject_dependencies = [_extract_package_name(dep) for dep in raw_deps]

        return CollectProjectDependencyInfoRunResult(
            package_name=package_name,
            package_kind=package_kind,
            pyproject_dependencies=pyproject_dependencies,
        )
