import pathlib
import types

import pytest

from fine_envs import dump_config_action
from fine_envs.dump_config_save_handler import DumpConfigSaveHandler
from finecode_extension_api import code_action
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


def _payload(tmp_path: pathlib.Path) -> dump_config_action.DumpConfigRunPayload:
    return dump_config_action.DumpConfigRunPayload(
        source_file_path=path_to_resource_uri(tmp_path / "pyproject.toml"),
        project_raw_config={},
        target_file_path=path_to_resource_uri(
            tmp_path / "finecode_config_dump" / "pyproject.toml"
        ),
    )


async def test_save_handler_writes_prepared_content_once(
    tmp_path: pathlib.Path,
) -> None:
    """The save handler writes exactly the content earlier handlers prepared
    and saves the file once — the dump has no unformatted window and no second
    write.
    """
    session = _FakeSession()
    handler = DumpConfigSaveHandler(
        file_manager=_FakeFileManager(), file_editor=_FakeFileEditor(session)
    )
    run_context = types.SimpleNamespace(
        raw_config_dump={}, config_dump_content="formatted dump"
    )

    await handler.run(_payload(tmp_path), run_context)

    assert session.saved == [
        (tmp_path / "finecode_config_dump" / "pyproject.toml", "formatted dump")
    ]


async def test_save_handler_without_rendered_content_fails(
    tmp_path: pathlib.Path,
) -> None:
    """Without the dump_config handler having rendered the dump there is nothing
    to write; failing names the missing handler instead of writing nothing."""
    session = _FakeSession()
    handler = DumpConfigSaveHandler(
        file_manager=_FakeFileManager(), file_editor=_FakeFileEditor(session)
    )
    run_context = types.SimpleNamespace(raw_config_dump={}, config_dump_content=None)

    with pytest.raises(code_action.ActionFailedException):
        await handler.run(_payload(tmp_path), run_context)

    assert session.saved == []
