"""A fake `pi --mode rpc` that speaks the JSONL protocol on stdin/stdout.

Lets the handler be tested end to end -- real subprocess, real pipes, real
framing -- with no model call, no cost and no variability. The script is driven
by a JSON *scenario* passed as argv[1]: a list of steps, each either a frame to
emit or an instruction to wait for one from the client.

Kept as a standalone script rather than a fixture because the thing under test
is precisely the subprocess boundary.
"""

import atexit
import json
import os
import signal
import subprocess
import sys
import time


def _emit(frame: dict) -> None:
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


def _record(scenario: dict, entry: dict) -> None:
    path = scenario.get("record_path")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as record:
        record.write(json.dumps(entry) + "\n")


def _read_frame() -> dict | None:
    line = sys.stdin.readline()
    if not line:
        return None
    try:
        return json.loads(line)
    except ValueError:
        return None


def main() -> int:
    scenario = json.loads(sys.argv[1])
    exit_code = scenario.get("exit_code", 0)

    # The driver-side facts (argv, prompt) are written on exit rather than when
    # they are observed, and only when a scenario asks for them, so the records
    # a scenario already inspects by position or by whole-list equality keep
    # their shape. Tests read these by key.
    driver_records: list[dict] = []
    if scenario.get("record_driver"):
        driver_records.append({"argv": sys.argv[2:]})

    def _flush_driver_records() -> None:
        for entry in driver_records:
            _record(scenario, entry)

    atexit.register(_flush_driver_records)

    for step in scenario["steps"]:
        action = step["do"]

        if action == "await_prompt":
            frame = _read_frame()
            if frame is None or frame.get("type") != "prompt":
                sys.stderr.write(f"expected a prompt, got {frame}\n")
                return 90
            if scenario.get("record_driver"):
                driver_records.append({"prompt": frame["message"]})

        elif action == "emit":
            _emit(step["frame"])

        elif action == "emit_raw":
            sys.stdout.write(step["text"])
            sys.stdout.flush()

        elif action == "await_ui_response":
            frame = _read_frame()
            if frame is None or frame.get("type") != "extension_ui_response":
                sys.stderr.write(f"expected a ui response, got {frame}\n")
                return 91
            # Recorded to a file rather than echoed on a pipe: stdout is the
            # protocol stream and must stay clean, and stderr is consumed by the
            # handler's own accumulator where the test cannot reach it.
            _record(scenario, {"ui_response": frame})

        elif action == "await_abort":
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                frame = _read_frame()
                if frame is None:
                    sys.stderr.write("stdin closed before abort\n")
                    return 92
                if frame.get("type") == "abort":
                    _record(scenario, {"aborted": True})
                    break
            else:
                return 93

        elif action == "answer_session_stats":
            frame = _read_frame()
            if frame is None or frame.get("type") != "get_session_stats":
                sys.stderr.write(f"expected a stats request, got {frame}\n")
                return 94
            _emit(
                {
                    "type": "response",
                    "command": "get_session_stats",
                    "success": step.get("success", True),
                    "data": step.get("data"),
                }
            )

        elif action == "stderr":
            sys.stderr.write(step["text"])
            sys.stderr.flush()

        elif action == "spawn_child":
            # A child of the agent's own, the way pi's own tools leave one
            # behind. Recorded so a test can check the teardown reached the tree
            # and not just the process the handler spawned.
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(300)"]
            )
            _record(scenario, {"pids": {"agent": os.getpid(), "child": child.pid}})

        elif action == "ignore_sigterm":
            # An agent that will not take the polite exit, so the teardown has
            # to reach its last rung.
            signal.signal(signal.SIGTERM, signal.SIG_IGN)

        elif action == "sleep":
            time.sleep(step["seconds"])

        elif action == "exit":
            return step.get("code", 0)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
