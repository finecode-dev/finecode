import dataclasses
import pathlib

from finecode_extension_api import code_action, textstyler


@dataclasses.dataclass
class LockDependenciesRunPayload(code_action.RunActionPayload):
    # Path to the artifact definition file (e.g. pyproject.toml, package.json).
    src_artifact_def_path: pathlib.Path

    # Output path for the lock file (e.g. pylock.toml, package-lock.json).
    output_path: pathlib.Path


class LockDependenciesRunContext(
    code_action.RunActionContext[LockDependenciesRunPayload]
): ...


@dataclasses.dataclass
class LockDependenciesRunResult(code_action.RunActionResult):
    lock_file_path: pathlib.Path

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, LockDependenciesRunResult):
            return

        self.lock_file_path = other.lock_file_path

    def to_text(self) -> str | textstyler.StyledText:
        return f"Locked dependencies to: {self.lock_file_path}"

    @property
    def return_code(self) -> code_action.RunReturnCode:
        return code_action.RunReturnCode.SUCCESS


class LockDependenciesAction(
    code_action.Action[
        LockDependenciesRunPayload,
        LockDependenciesRunContext,
        LockDependenciesRunResult,
    ]
):
    PAYLOAD_TYPE = LockDependenciesRunPayload
    RUN_CONTEXT_TYPE = LockDependenciesRunContext
    RESULT_TYPE = LockDependenciesRunResult
