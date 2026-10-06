import dataclasses
import sys

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.resource_uri import ResourceUri, resource_uri_to_path


@dataclasses.dataclass
class EnvInfo:
    name: str
    venv_dir_path: ResourceUri
    project_def_path: ResourceUri
    dependencies_override: list[str] = dataclasses.field(default_factory=list)
    interpreter: str | None = None
    """Canonical "<impl>@<version>" interpreter request (e.g. "cpython@3.11").
    None means the default interpreter — an ordinary single-interpreter env."""


def env_label(env: EnvInfo) -> str:
    """Human-readable ``<project>/<env_name>`` label for progress/log messages.

    The env name alone is ambiguous when one dispatch call spans multiple
    projects (e.g. prepare-envs' dev_workspace bootstrap step creates one
    same-named "dev_workspace" env per subproject). ``venv_dir_path`` is
    always ``<project_dir>/.venvs/<env_name>`` (every caller must supply it
    to locate the venv), so the project directory name is recoverable for
    any caller without a caller-supplied label.
    """
    project_dir = resource_uri_to_path(env.venv_dir_path).parent.parent
    return f"{project_dir.name}/{env.name}"


@dataclasses.dataclass
class CreateEnvsRunPayload(code_action.RunActionPayload):
    envs: list[EnvInfo] | None = None
    """Explicit list of environments to create. ``None`` means handlers discover envs, empty list means explicit no-op."""
    recreate: bool = False
    """Remove and recreate existing environments from scratch even if they are already valid."""
    env_names: list[str] | None = None
    """Filter: when set, only environments whose name is in this list will be created. Applied during discovery only."""


class CreateEnvsRunContext(code_action.RunActionContext[CreateEnvsRunPayload]):
    def __init__(
        self,
        run_id: int,
        initial_payload: CreateEnvsRunPayload,
        meta: code_action.RunActionMeta,
        info_provider: code_action.RunContextInfoProvider,
        progress_sender: code_action.ProgressSender = code_action._NOOP_PROGRESS_SENDER,
    ) -> None:
        super().__init__(
            run_id=run_id,
            initial_payload=initial_payload,
            meta=meta,
            info_provider=info_provider,
            progress_sender=progress_sender,
        )

        self.envs: list[EnvInfo] | None = None

    async def init(self) -> None:
        if self.initial_payload.envs is not None:
            self.envs = list(self.initial_payload.envs)


@dataclasses.dataclass
class CreateEnvsRunResult(code_action.RunActionResult):
    errors: list[str]
    created: bool = True
    """Whether a new virtualenv was actually built, vs. a valid one already existing
    and creation being skipped. Meaningful only for a single-env `CreateEnvAction`
    result — unused/inert on the batch `CreateEnvsAction` aggregate, which nobody
    reads this field on."""

    @override
    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, CreateEnvsRunResult):
            return
        self.errors += other.errors

    def to_text(self) -> str | textstyler.StyledText:
        return "\n".join(self.errors)

    @property
    def return_code(self) -> code_action.RunReturnCode:
        if len(self.errors) == 0:
            return code_action.RunReturnCode.SUCCESS
        else:
            return code_action.RunReturnCode.ERROR


class CreateEnvsAction(
    code_action.Action[CreateEnvsRunPayload, CreateEnvsRunContext, CreateEnvsRunResult]
):
    """Create environments for the workspace(without installing dependencies, only environment)."""

    DESCRIPTION = "Create environments for the workspace (without installing dependencies, only environments)."
    PAYLOAD_TYPE = CreateEnvsRunPayload
    RUN_CONTEXT_TYPE = CreateEnvsRunContext
    RESULT_TYPE = CreateEnvsRunResult
