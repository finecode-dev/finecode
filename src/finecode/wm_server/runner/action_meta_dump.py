from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import typing
from pathlib import Path

DUMP_TIMEOUTS_SEC: tuple[float, ...] = (30.0, 90.0)

DumpKind = typing.Literal["ok", "skew", "env_unusable", "timeout"]


@dataclasses.dataclass(frozen=True)
class DumpOutcome:
    kind: DumpKind
    document: dict | None
    reason: str = ""


async def run_dump(
    python_cmd: str,
    project_dir: Path,
    sources: list[str],
    *,
    attempt_timeout: float,
) -> DumpOutcome:
    argv = [
        python_cmd,
        "-m",
        "finecode_extension_runner.cli",
        "dump-action-meta",
        f"--project-path={project_dir}",
    ]
    env = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=project_dir,
            env=env,
        )
    except OSError as exception:
        return DumpOutcome(
            kind="env_unusable", document=None, reason=f"cannot spawn dump: {exception}"
        )
    payload = json.dumps({"sources": list(sources)}).encode()
    try:
        raw_stdout, raw_stderr = await asyncio.wait_for(
            process.communicate(input=payload), timeout=attempt_timeout
        )
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        return DumpOutcome(
            kind="timeout",
            document=None,
            reason=f"dump did not finish within {attempt_timeout:g}s",
        )
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise
    stderr = raw_stderr.decode(errors="replace")
    if process.returncode == 2 and "No such command" in stderr:
        return DumpOutcome(
            kind="skew",
            document=None,
            reason=f"ER has no dump-action-meta subcommand: {stderr.strip()[-300:]}",
        )
    if process.returncode != 0:
        return DumpOutcome(
            kind="env_unusable",
            document=None,
            reason=f"dump exited {process.returncode}: {stderr.strip()[-300:]}",
        )
    try:
        document = json.loads(raw_stdout.decode())
    except (UnicodeDecodeError, ValueError) as exception:
        return DumpOutcome(
            kind="env_unusable",
            document=None,
            reason=f"dump stdout is not JSON: {exception}",
        )
    if not isinstance(document, dict) or document.get("format") != 1:
        return DumpOutcome(
            kind="skew",
            document=document if isinstance(document, dict) else None,
            reason=f"unsupported dump format {document.get('format') if isinstance(document, dict) else type(document).__name__}",
        )
    if not isinstance(document.get("entries"), dict) or not isinstance(
        document.get("failures"), dict
    ):
        return DumpOutcome(
            kind="env_unusable",
            document=None,
            reason="dump document is missing entries/failures",
        )
    return DumpOutcome(kind="ok", document=document)
