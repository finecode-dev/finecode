import dataclasses
import pathlib
import typing

from fine_python_lang import build_python_artifact_action
from fine_src_artifacts import build_artifact_action
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    icommandrunner,
    ilogger,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import (
    path_to_resource_uri,
    resource_uri_to_path,
)

from ._uv_common import get_uv_executable


@dataclasses.dataclass
class UvBuildPythonArtifactHandlerConfig(code_action.ActionHandlerConfig): ...


class UvBuildPythonArtifactHandler(
    code_action.ActionHandler[
        build_python_artifact_action.BuildPythonArtifactAction,
        UvBuildPythonArtifactHandlerConfig,
    ]
):
    """Build Python distributions with ``uv build``."""

    def __init__(
        self,
        config: UvBuildPythonArtifactHandlerConfig,
        command_runner: icommandrunner.ICommandRunner,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        logger: ilogger.ILogger,
    ) -> None:
        self.config = config
        self.command_runner = command_runner
        self.project_info_provider = project_info_provider
        self.logger = logger

    async def run(
        self,
        payload: build_python_artifact_action.BuildPythonArtifactRunPayload,
        run_context: build_python_artifact_action.BuildPythonArtifactRunContext,
    ) -> build_artifact_action.BuildArtifactRunResult:
        if payload.src_artifact_def_path is None:
            project_def_path = self.project_info_provider.get_current_project_def_path()
        else:
            project_def_path = resource_uri_to_path(payload.src_artifact_def_path)

        project_dir = project_def_path.parent

        if payload.output_dir is not None:
            output_dir = resource_uri_to_path(payload.output_dir)
        else:
            output_dir = project_dir / "dist"

        uv_executable = get_uv_executable()
        cmd = self._construct_build_cmd(
            uv_executable=uv_executable,
            output_dir=output_dir,
            distributions=payload.distributions,
        )
        self.logger.info(f"Building Python artifact in {project_dir} with: {cmd!r}")

        process = await self.command_runner.run(cmd=cmd, cwd=project_dir)
        await process.wait_for_end()

        exit_code = process.get_exit_code()
        if exit_code != 0:
            error_output = process.get_error_output()
            raise code_action.ActionFailedException(
                f"Build failed with exit code {exit_code}: {error_output}"
            )

        build_output_paths = self._parse_built_paths(
            output=process.get_output() + "\n" + process.get_error_output(),
            cwd=project_dir,
        )
        if not build_output_paths:
            raise code_action.ActionFailedException(
                f"uv build succeeded but reported no output path (cmd: {cmd!r})"
            )

        self.logger.info(f"Build completed. Output: {build_output_paths}")

        return build_artifact_action.BuildArtifactRunResult(
            src_artifact_def_path=path_to_resource_uri(project_def_path),
            build_output_paths=[
                path_to_resource_uri(path) for path in build_output_paths
            ],
        )

    def _construct_build_cmd(
        self,
        uv_executable: pathlib.Path,
        output_dir: pathlib.Path,
        distributions: list[typing.Literal["sdist", "wheel"]] | None,
    ) -> list[str]:
        cmd: list[str] = [str(uv_executable), "build"]
        if distributions is not None:
            if "sdist" in distributions and "wheel" not in distributions:
                cmd.append("--sdist")
            elif "wheel" in distributions and "sdist" not in distributions:
                cmd.append("--wheel")
        cmd.extend(["--out-dir", str(output_dir)])
        return cmd

    def _parse_built_paths(self, output: str, cwd: pathlib.Path) -> list[pathlib.Path]:
        build_output_paths: list[pathlib.Path] = []
        for line in output.splitlines():
            if not line.startswith("Successfully built "):
                continue
            built_path = pathlib.Path(line[len("Successfully built ") :].strip())
            # uv prints the path relative to the build cwd when the output dir
            # is inside it, and absolute otherwise.
            if not built_path.is_absolute():
                built_path = cwd / built_path
            build_output_paths.append(built_path)
        return build_output_paths
