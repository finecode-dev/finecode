import pathlib
import types

from fine_envs import dump_config_action
from fine_envs.dump_config_save_handler import (
    DumpConfigSaveHandler,
    _attribution_comment,
)
from finecode_extension_api.resource_uri import path_to_resource_uri


class _FakeFileManager:
    def __init__(self) -> None:
        self.created_dirs: list[pathlib.Path] = []

    async def create_dir(self, dir_path: pathlib.Path) -> None:
        self.created_dirs.append(dir_path)


class _FakeSession:
    def __init__(self) -> None:
        self.saved: list[tuple[pathlib.Path, str]] = []

    async def save_file(self, file_path: pathlib.Path, file_content: str) -> None:
        self.saved.append((file_path, file_content))


class _FakeFileEditor:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def session(self, author):
        return _FakeSessionCM(self._session)


class _FakeSessionCM:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeSession:
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakeProjectInfoProvider:
    def __init__(self, selection: dict[str, list[str]]) -> None:
        self._selection = selection

    async def get_workspace_extra_selection(self) -> dict[str, list[str]]:
        return self._selection


async def test_save_handler_uses_provider_selection_for_attribution(
    tmp_path: pathlib.Path,
) -> None:
    selection = {"finecode-dev-common-preset": ["lint_fix"]}
    raw_config = {
        "dependency-groups": {
            "runtime": ["finecode_dev_common_preset[lint_fix]~=0.3.0a0"]
        }
    }
    session = _FakeSession()
    handler = DumpConfigSaveHandler(
        file_manager=_FakeFileManager(),
        file_editor=_FakeFileEditor(session),
        project_info_provider=_FakeProjectInfoProvider(selection),
    )
    payload = dump_config_action.DumpConfigRunPayload(
        source_file_path=path_to_resource_uri(tmp_path / "pyproject.toml"),
        project_raw_config=raw_config,
        target_file_path=path_to_resource_uri(
            tmp_path / "finecode_config_dump" / "pyproject.toml"
        ),
    )
    run_context = types.SimpleNamespace(raw_config_dump=raw_config)

    await handler.run(payload, run_context)

    assert session.saved
    saved_content = session.saved[0][1]
    assert saved_content.startswith("# Dependency specs rewritten by")
    assert "finecode-workspace-user.toml" in saved_content


def test_attribution_comment_names_selection_file_and_extra() -> None:
    """A rewritten spec is attributed to the selection file and its extra."""
    selection = {"finecode-dev-common-preset": ["lint_fix"]}
    raw_config = {
        "dependency-groups": {
            "runtime": ["finecode_dev_common_preset[lint_fix]~=0.3.0a0"]
        }
    }

    comment = _attribution_comment(selection, raw_config)

    assert "finecode-workspace-user.toml" in comment
    assert "lint_fix" in comment
    assert "finecode_dev_common_preset[lint_fix]~=0.3.0a0" in comment


def test_attribution_comment_empty_selection_is_empty() -> None:
    raw_config = {
        "dependency-groups": {
            "runtime": ["finecode_dev_common_preset[lint_fix]~=0.3.0a0"]
        }
    }

    assert _attribution_comment({}, raw_config) == ""


def test_attribution_comment_no_matching_spec_is_empty() -> None:
    selection = {"finecode-dev-common-preset": ["lint_fix"]}
    raw_config = {"dependency-groups": {"runtime": ["other~=1.0"]}}

    assert _attribution_comment(selection, raw_config) == ""
