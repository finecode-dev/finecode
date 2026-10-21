"""A fake `codex exec` that speaks the real JSONL framing.

Lets the handler be tested end to end -- real subprocess, real pipes -- with
no model call and no cost. The script takes three fixed arguments (scenario,
record path, JSON-encoded answer) followed by the real command the production
builder assembled; answer contents travel through the fixed argument, never
through shell code.
"""

import json
import os
import pathlib
import signal
import subprocess
import sys
import time
from typing import Any

# The fake is spawned with the run's project as cwd, so the test package is
# reachable only through the fake's own directory.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tests.fake_codex_support import FakeScenario


def _emit(frame: dict[str, Any]) -> None:
    _ = sys.stdout.write(json.dumps(frame) + "\n")
    _ = sys.stdout.flush()


def _flag_value(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


def main() -> int:
    scenario = FakeScenario(sys.argv[1])
    record_path = sys.argv[2]
    answer_arg = sys.argv[3]
    rest = sys.argv[4:]
    answer = json.loads(answer_arg)

    prompt = sys.stdin.read()

    answer_path = _flag_value(rest, "--output-last-message")
    assert answer_path is not None
    schema_path = _flag_value(rest, "--output-schema")
    schema = None
    if schema_path is not None:
        with open(schema_path, encoding="utf-8") as handle:
            schema = json.load(handle)

    child: subprocess.Popen[str] | None = None
    if scenario in (
        FakeScenario.IGNORE_TERM,
        FakeScenario.LEAVE_CHILD,
    ):
        if scenario is FakeScenario.IGNORE_TERM:
            _ = signal.signal(signal.SIGTERM, signal.SIG_IGN)
        # Detached pipes: the leader exiting must not close the child's end,
        # so group liveness is tested independently of pipe EOF.
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    if record_path:
        record: dict[str, Any] = {
            "argv": rest,
            "cwd": os.getcwd(),
            "prompt": prompt,
            "schema": schema,
            "schema_path": schema_path,
            "answer_path": answer_path,
            "agent_pid": os.getpid(),
            "child_pid": child.pid if child is not None else None,
        }
        with open(record_path, "w", encoding="utf-8") as handle:
            _ = handle.write(json.dumps(record))

    def write_answer(text: str) -> None:
        assert answer_path is not None
        with open(answer_path, "w", encoding="utf-8") as handle:
            _ = handle.write(text)

    def write_text_answer() -> None:
        write_answer(answer if isinstance(answer, str) else answer_arg)

    if scenario is FakeScenario.SUCCESS:
        _emit({"type": "thread.started", "thread_id": "thread-1"})
        _emit({"type": "turn.started"})
        _emit(
            {
                "type": "item.started",
                "item": {"id": "1", "type": "command_execution"},
            }
        )
        _emit(
            {
                "type": "item.completed",
                "item": {
                    "id": "2",
                    "type": "agent_message",
                    "text": "streamed interim",
                },
            }
        )
        _emit(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                },
            }
        )
        write_text_answer()
        return 0

    if scenario is FakeScenario.NOISE:
        _ = sys.stdout.write("warning: something\n")
        _ = sys.stdout.flush()
        _ = sys.stdout.buffer.write(b"\xff\xfe noisy bytes\n")
        _ = sys.stdout.buffer.flush()
        _emit({"type": "future.event", "extra": [1, 2]})
        _emit(
            {
                "type": "thread.started",
                "thread_id": "thread-noise",
                "extra": {"nested": True},
            }
        )
        _emit(
            {
                "type": "item.updated",
                "item": {"id": "9", "type": "file_change", "text": "x"},
                "extra_key": "tolerated",
            }
        )
        _emit(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 12,
                    "cached_input_tokens": 34,
                    "output_tokens": 56,
                    "reasoning_output_tokens": 7,
                    "extra": 1,
                },
            }
        )
        write_text_answer()
        return 0

    if scenario is FakeScenario.FAILED:
        _emit({"type": "thread.started", "thread_id": "thread-fail"})
        _emit(
            {
                "type": "item.completed",
                "item": {"id": "3", "type": "agent_message", "text": "partial work"},
            }
        )
        _emit(
            {
                "type": "turn.failed",
                "error": {"message": "boom"},
                "usage": {"input_tokens": 5, "output_tokens": 6},
            }
        )
        return 0

    if scenario is FakeScenario.NO_TERMINAL:
        _emit({"type": "thread.started", "thread_id": "thread-hung"})
        _emit(
            {
                "type": "item.completed",
                "item": {"id": "4", "type": "agent_message", "text": "halfway"},
            }
        )
        return 0

    if scenario is FakeScenario.NONZERO:
        _emit({"type": "thread.started", "thread_id": "thread-nonzero"})
        _emit({"type": "turn.started"})
        _emit(
            {
                "type": "item.completed",
                "item": {
                    "id": "7",
                    "type": "agent_message",
                    "text": "before the crash",
                },
            }
        )
        _emit({"type": "turn.completed", "usage": {"input_tokens": 1}})
        write_text_answer()
        _ = sys.stderr.write("auth expired\n")
        _ = sys.stderr.flush()
        return 1

    if scenario is FakeScenario.RETRY:
        _emit({"type": "thread.started", "thread_id": "thread-retry"})
        _emit({"type": "error", "message": "retrying after a hiccup"})
        _emit(
            {
                "type": "item.completed",
                "item": {"id": "5", "type": "error", "text": "tool blew up"},
            }
        )
        _emit({"type": "turn.completed", "usage": {"input_tokens": 2}})
        write_text_answer()
        return 0

    if scenario is FakeScenario.EMPTY:
        _emit({"type": "thread.started", "thread_id": "thread-empty"})
        _emit({"type": "turn.completed"})
        write_answer("")
        return 0

    if scenario is FakeScenario.MISSING_ANSWER:
        _emit({"type": "thread.started", "thread_id": "thread-missing"})
        _emit(
            {
                "type": "item.completed",
                "item": {"id": "6", "type": "agent_message", "text": "fallback text"},
            }
        )
        _emit({"type": "turn.completed"})
        return 0

    if scenario is FakeScenario.STRUCTURED_VALID:
        _emit({"type": "thread.started", "thread_id": "thread-structured"})
        _emit({"type": "turn.completed"})
        write_answer(answer_arg)
        return 0

    if scenario is FakeScenario.STRUCTURED_INVALID:
        _emit({"type": "thread.started", "thread_id": "thread-bad-json"})
        _emit({"type": "turn.completed"})
        write_answer(answer if isinstance(answer, str) else answer_arg)
        return 0

    if scenario is FakeScenario.HANG:
        time.sleep(300)
        return 0

    if scenario is FakeScenario.IGNORE_TERM:
        time.sleep(300)
        return 0

    if scenario is FakeScenario.LEAVE_CHILD:
        _emit({"type": "thread.started", "thread_id": "thread-child"})
        _emit({"type": "turn.completed"})
        write_text_answer()
        return 0

    if scenario is FakeScenario.BAD_UTF8:
        _emit({"type": "thread.started", "thread_id": "thread-bad-utf8"})
        _ = sys.stdout.flush()
        _ = sys.stdout.buffer.write(b"x" * (8 * 1024 * 1024 + 1024))
        _ = sys.stdout.buffer.flush()
        time.sleep(30)
        return 0

    raise AssertionError(f"unknown scenario: {scenario}")


if __name__ == "__main__":
    sys.exit(main())
