from __future__ import annotations

import pathlib

from finecode.wm_server import domain
from finecode.wm_server.runner import runner_client, runner_manager


class _FakeClient:
    def __init__(self) -> None:
        self.notifications: list[tuple[str, object]] = []

    def notify(self, method: str, params: object | None = None) -> None:
        self.notifications.append((method, params))


async def test_send_opened_files_forwards_known_content_to_a_restarted_runner(
    tmp_path: pathlib.Path,
) -> None:
    """A runner that (re)starts while a file is already open must get its real content.

    The WM already knows this file's content from the IDE's own `didOpen`
    (`WorkspaceContext.opened_documents`). If that content is dropped while
    re-notifying a (re)started runner, the ER seeds an empty buffer instead —
    it no longer falls back to reading the file from disk once it trusts the
    wire notification's content directly.
    """
    fake_client = _FakeClient()
    runner = runner_client.ExtensionRunnerInfo(
        working_dir_path=tmp_path,
        env_name="dev",
        status=domain.ExtensionRunnerStatus.RUNNING,
        client=fake_client,  # type: ignore[arg-type]
    )
    opened_file = tmp_path / "a.py"
    document_info = domain.TextDocumentInfo(
        uri=f"file://{opened_file.as_posix()}", version="3", text="x = 1\n"
    )

    await runner_manager.send_opened_files(runner=runner, opened_files=[document_info])

    assert len(fake_client.notifications) == 1
    method, params = fake_client.notifications[0]
    assert method == "textDocument/didOpen"
    assert params.text_document.text == "x = 1\n"
