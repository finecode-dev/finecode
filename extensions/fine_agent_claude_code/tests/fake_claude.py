"""A fake `claude -p --output-format stream-json` that speaks the real JSONL.

Lets the handler be tested end to end -- real subprocess, real pipes, real
framing -- with no model call, no cost and no variability. The script is driven
by a JSON *scenario* passed as argv[1]: a list of steps, each either a frame to
emit or an instruction to consume the prompt the handler sends.

Kept as a standalone script rather than a fixture because the thing under test
is precisely the subprocess boundary.
"""

import json
import os
import signal
import subprocess
import sys
import time

_SESSION = "11111111-2222-3333-4444-555555555555"


def _emit(frame: dict) -> None:
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


def _record(scenario: dict, entry: dict) -> None:
    path = scenario.get("record_path")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as record:
        record.write(json.dumps(entry) + "\n")


def main() -> int:
    scenario = json.loads(sys.argv[1])

    if scenario.get("record_driver"):
        # The CLI flags the handler assembled, gated so scenarios that inspect
        # the whole record list keep its shape. Tests read this by key.
        _record(scenario, {"argv": sys.argv[2:]})

    for step in scenario["steps"]:
        action = step["do"]

        if action == "read_prompt":
            # The real CLI reads the prompt from stdin until EOF when no
            # prompt argument is given, which is how the handler sends it.
            _record(scenario, {"prompt": sys.stdin.read()})

        elif action == "emit":
            _emit(step["frame"])

        elif action == "emit_raw":
            sys.stdout.write(step["text"])
            sys.stdout.flush()

        elif action == "init":
            _emit(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": _SESSION,
                    "model": step.get("model", "claude-opus-5"),
                    "cwd": step.get("cwd", "/workspace"),
                    "permissionMode": "default",
                }
            )

        elif action == "stderr":
            sys.stderr.write(step["text"])
            sys.stderr.flush()

        elif action == "spawn_child":
            # A child of the agent's own, the way the real CLI's Bash tool
            # leaves one behind. Recorded so a test can check the teardown
            # reached the tree and not just the process the handler spawned.
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

    return scenario.get("exit_code", 0)


if __name__ == "__main__":
    sys.exit(main())
