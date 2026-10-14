from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import psutil
import pytest

from finecode.wm_server.runner import action_meta_dump

_FAKE_CLI = """\
import json
import os
import sys
from pathlib import Path


def main() -> None:
    data = json.load(sys.stdin)
    sources = data.get("sources", [])
    record = {
        "argv": sys.argv,
        "cwd": os.getcwd(),
        "virtual_env_present": "VIRTUAL_ENV" in os.environ,
    }
    Path("dump_record.json").write_text(json.dumps(record))
    if any("no-command" in s for s in sources):
        print("Error: No such command 'dump-action-meta'.", file=sys.stderr)
        sys.exit(2)
    if any("skew-format" in s for s in sources):
        json.dump({"format": 2}, sys.stdout)
        return
    if any("exit-1" in s for s in sources):
        print("boom", file=sys.stderr)
        sys.exit(1)
    if any("garbage" in s for s in sources):
        print("not json at all")
        return
    if any("sleep" in s for s in sources):
        import time

        time.sleep(10)
    json.dump(
        {
            "format": 1,
            "startedNs": 1,
            "actionMetaFile": None,
            "header": {},
            "entries": {},
            "failures": {},
        },
        sys.stdout,
    )


if __name__ == "__main__":
    main()
"""


@pytest.fixture()
def fake_project(tmp_path: Path) -> Path:
    package = tmp_path / "finecode_extension_runner"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text(_FAKE_CLI, encoding="utf-8")
    return tmp_path


def _record(project: Path) -> dict:
    return json.loads((project / "dump_record.json").read_text(encoding="utf-8"))


async def test_ok_document(fake_project: Path) -> None:
    """A valid dump document resolves, so healthy envs answer from their own interpreter."""
    outcome = await action_meta_dump.run_dump(
        sys.executable, fake_project, ["ok-source"], attempt_timeout=30.0
    )

    assert outcome.kind == "ok"
    assert outcome.document is not None
    assert outcome.document["format"] == 1


async def test_skew_without_subcommand(fake_project: Path) -> None:
    """An older ER without the subcommand hides its subactions instead of failing loudly."""
    outcome = await action_meta_dump.run_dump(
        sys.executable, fake_project, ["no-command"], attempt_timeout=30.0
    )

    assert outcome.kind == "skew"


async def test_skew_on_other_format(fake_project: Path) -> None:
    """A future document the WM cannot read is skew, not a broken env."""
    outcome = await action_meta_dump.run_dump(
        sys.executable, fake_project, ["skew-format"], attempt_timeout=30.0
    )

    assert outcome.kind == "skew"


async def test_env_unusable_on_exit_1(fake_project: Path) -> None:
    """A crashing dump env hides its subactions until prepare-envs fixes it."""
    outcome = await action_meta_dump.run_dump(
        sys.executable, fake_project, ["exit-1"], attempt_timeout=30.0
    )

    assert outcome.kind == "env_unusable"


async def test_env_unusable_on_garbage_stdout(fake_project: Path) -> None:
    """A dump that does not speak JSON is unusable, so nothing it says is trusted."""
    outcome = await action_meta_dump.run_dump(
        sys.executable, fake_project, ["garbage"], attempt_timeout=30.0
    )

    assert outcome.kind == "env_unusable"


async def test_timeout_kills_the_process(fake_project: Path) -> None:
    """A slow dump cannot hold a slot forever, and it leaves no orphan behind it."""
    before = len(psutil.Process().children())

    outcome = await action_meta_dump.run_dump(
        sys.executable, fake_project, ["sleep"], attempt_timeout=0.2
    )

    assert outcome.kind == "timeout"
    assert len(psutil.Process().children()) <= before


async def test_cancellation_kills_the_process(fake_project: Path) -> None:
    """Cancelling a dump kills its interpreter, so shutdown never waits on a dump."""
    before = len(psutil.Process().children())
    task = asyncio.ensure_future(
        action_meta_dump.run_dump(
            sys.executable, fake_project, ["sleep"], attempt_timeout=30.0
        )
    )
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(psutil.Process().children()) <= before


async def test_argv_cwd_and_environment(
    fake_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dump runs like an ER start, so it sees the same imports the ER would."""
    seen: dict = {}
    real_exec = asyncio.create_subprocess_exec

    async def _recording(*args, **kwargs):
        seen["argv"] = list(args)
        seen["kwargs"] = kwargs
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _recording)
    monkeypatch.setenv("VIRTUAL_ENV", "/tmp/should-not-propagate")
    outcome = await action_meta_dump.run_dump(
        sys.executable, fake_project, ["ok-source"], attempt_timeout=30.0
    )

    assert outcome.kind == "ok"
    assert seen["argv"] == [
        sys.executable,
        "-m",
        "finecode_extension_runner.cli",
        "dump-action-meta",
        f"--project-path={fake_project}",
    ]
    assert seen["kwargs"]["cwd"] == fake_project
    assert "VIRTUAL_ENV" not in seen["kwargs"]["env"]
    record = _record(fake_project)
    assert Path(record["cwd"]).resolve() == fake_project.resolve()
    assert record["virtual_env_present"] is False
