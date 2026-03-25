import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.artifact import build_artifact_action
from finecode_extension_api.interfaces import (
    icommandrunner,
    iextensionrunnerinfoprovider,
    ilogger,
    iprojectinfoprovider,
)


@dataclasses.dataclass
class BuildArtifactPyHandlerConfig(code_action.ActionHandlerConfig): ...


class BuildArtifactPyHandler(
    code_action.ActionHandler[
        build_artifact_action.BuildArtifactAction,
        BuildArtifactPyHandlerConfig,
    ]
):
    def __init__(
        self,
        config: BuildArtifactPyHandlerConfig,
        command_runner: icommandrunner.ICommandRunner,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        extension_runner_info_provider: iextensionrunnerinfoprovider.IExtensionRunnerInfoProvider,
        logger: ilogger.ILogger,
    ) -> None:
        self.config = config
        self.command_runner = command_runner
        self.project_info_provider = project_info_provider
        self.extension_runner_info_provider = extension_runner_info_provider
        self.logger = logger

    async def run(
        self,
        payload: build_artifact_action.BuildArtifactRunPayload,
        run_context: build_artifact_action.BuildArtifactRunContext,
    ) -> build_artifact_action.BuildArtifactRunResult:
        # Use current project if src_artifact_def_path is not provided
        src_artifact_def_path = payload.src_artifact_def_path
        if src_artifact_def_path is None:
            src_artifact_def_path = (
                self.project_info_provider.get_current_project_def_path()
            )

        # Get the project directory (parent of pyproject.toml)
        project_dir = src_artifact_def_path.parent

        self.logger.info(f"Building artifact in {project_dir}")

        # Get the python interpreter from the current venv
        venv_dir = self.extension_runner_info_provider.get_current_venv_dir_path()
        python_path = self.extension_runner_info_provider.get_venv_python_interpreter(
            venv_dir
        )

        # Run python -m build
        process = await self.command_runner.run(
            cmd=f"{python_path} -m build",
            cwd=project_dir,
        )
        await process.wait_for_end()

        exit_code = process.get_exit_code()
        if exit_code != 0:
            error_output = process.get_error_output()
            raise code_action.ActionFailedException(
                f"Build failed with exit code {exit_code}: {error_output}"
            )

        # Parse the build output to get the produced file names
        # Example line: "Successfully built pkg-1.0.tar.gz and pkg-1.0-py3-none-any.whl"
        dist_dir = project_dir / "dist"
        build_output_paths = []

        output = process.get_output()
        for line in output.splitlines():
            if line.startswith("Successfully built "):
                files_part = line[len("Successfully built ") :]
                file_names = [f.strip() for f in files_part.split(" and ")]
                build_output_paths = [dist_dir / name for name in file_names]
                break

        if not build_output_paths:
            # Fallback: return the dist directory if parsing failed
            build_output_paths = [dist_dir]

        self.logger.info(f"Build completed. Output: {build_output_paths}")

        return build_artifact_action.BuildArtifactRunResult(
            src_artifact_def_path=src_artifact_def_path,
            build_output_paths=build_output_paths,
        )
