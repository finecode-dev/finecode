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


def _find_session_file(session_dir: str, session_id: str) -> str | None:
    """The one native session file under *session_dir* with this header id."""
    matches: list[str] = []
    try:
        entries = sorted(os.listdir(session_dir))
    except OSError:
        return None
    for entry in entries:
        if not entry.endswith(".jsonl"):
            continue
        path = os.path.join(session_dir, entry)
        try:
            with open(path, encoding="utf-8") as handle:
                first = handle.readline()
        except OSError:
            continue
        try:
            header = json.loads(first)
        except ValueError:
            continue
        if (
            isinstance(header, dict)
            and header.get("type") == "session"
            and header.get("id") == session_id
        ):
            matches.append(path)
    return matches[0] if len(matches) == 1 else None


def _emit_get_state(
    session_id: str | None, session_file: str | None, request_id: object
) -> None:
    """Answer one `get_state` command with this fake's actual session.

    Production IDs come only from the native store the fake maintains, never
    from a scenario-invented value, so a test that resumes a session proves
    the handler reached the same store.
    """
    _emit(
        {
            "type": "response",
            "command": "get_state",
            "id": request_id,
            "success": True,
            "data": {"sessionId": session_id, "sessionFile": session_file},
        }
    )


def main() -> int:
    scenario = json.loads(sys.argv[1])
    rest = sys.argv[2:]
    if "--version" in rest:
        sys.stdout.write(str(scenario.get("version", "1.3.0")) + "\n")
        sys.stdout.flush()
        return 0
    exit_code = scenario.get("exit_code", 0)
    session_id: str | None = None
    for flag in ("--session", "--session-id"):
        if flag in rest:
            idx = rest.index(flag)
            if idx + 1 < len(rest):
                session_id = rest[idx + 1]
    session_dir = scenario.get("session_dir") or os.environ.get(
        "PI_CODING_AGENT_SESSION_DIR"
    )
    resume = "--session" in rest
    session_file: str | None = None
    if resume:
        # Resume reopens the existing native session: appending to it proves
        # the continuation reached the same store. A missing session fails
        # here without creating anything, the way the native CLI refuses a
        # resume it cannot resolve.
        if session_id is not None and session_dir:
            session_file = _find_session_file(session_dir, session_id)
        if session_file is None:
            sys.stderr.write(f"no such pi session: {session_id}\n")
            sys.stderr.flush()
            return 3
    elif session_id is not None and session_dir:
        try:
            os.makedirs(session_dir, exist_ok=True)
            timestamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime())
            session_file = os.path.join(session_dir, f"{timestamp}_{session_id}.jsonl")
            header = {
                "type": "session",
                "version": 3,
                "id": session_id,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "cwd": os.getcwd(),
            }
            lines = [json.dumps(header) + "\n"]
            lines += [
                json.dumps(entry) + "\n" for entry in scenario.get("native_entries", [])
            ]
            with open(session_file, "w", encoding="utf-8") as handle:
                handle.writelines(lines)
        except OSError:
            session_file = None

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

    pending_get_state_id: object = None
    for step in scenario["steps"]:
        action = step["do"]

        if action == "await_prompt":
            frame = _read_frame()
            # The handler sends one `get_state` identity check per run. When
            # the scenario does not step it explicitly, answer it here with
            # the fake's actual session so established scenarios keep passing
            # unchanged; explicit `await_get_state` steps take precedence
            # because they run before this one.
            while frame is not None and frame.get("type") == "get_state":
                _emit_get_state(session_id, session_file, frame.get("id"))
                if scenario.get("record_driver"):
                    driver_records.append({"get_state_id": frame.get("id")})
                frame = _read_frame()
            if frame is None or frame.get("type") != "prompt":
                sys.stderr.write(f"expected a prompt, got {frame}\n")
                return 90
            if scenario.get("record_driver"):
                driver_records.append({"prompt": frame["message"]})
            if session_file is not None:
                try:
                    with open(session_file, "a", encoding="utf-8") as handle:
                        handle.write(
                            json.dumps(
                                {
                                    "type": "message",
                                    "id": "user-1",
                                    "message": {
                                        "role": "user",
                                        "content": frame.get("message", ""),
                                    },
                                }
                            )
                            + "\n"
                        )
                except OSError:
                    pass

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

        elif action == "await_get_state":
            frame = _read_frame()
            if frame is None or frame.get("type") != "get_state":
                sys.stderr.write(f"expected a get_state, got {frame}\n")
                return 95
            pending_get_state_id = frame.get("id")
            if scenario.get("record_driver"):
                driver_records.append({"get_state_id": pending_get_state_id})

        elif action == "answer_get_state":
            _emit_get_state(session_id, session_file, pending_get_state_id)

        elif action == "answer_get_state_wrong":
            _emit(
                {
                    "type": "response",
                    "command": "get_state",
                    "id": pending_get_state_id,
                    "success": True,
                    "data": {
                        "sessionId": step.get(
                            "wrong_id", "00000000-0000-4000-8000-000000000000"
                        ),
                        "sessionFile": session_file,
                    },
                }
            )

        elif action == "answer_get_state_failure":
            _emit(
                {
                    "type": "response",
                    "command": "get_state",
                    "id": pending_get_state_id,
                    "success": False,
                    "data": None,
                }
            )

        elif action == "answer_get_state_no_id":
            _emit(
                {
                    "type": "response",
                    "command": "get_state",
                    "id": pending_get_state_id,
                    "success": True,
                    "data": {"sessionFile": session_file},
                }
            )

        elif action == "answer_session_stats":
            frame = _read_frame()
            if frame is None or frame.get("type") != "get_session_stats":
                sys.stderr.write(f"expected a stats request, got {frame}\n")
                return 94
            data = step.get("data")
            if (
                isinstance(data, dict)
                and session_file is not None
                and not scenario.get("preserve_stats_ids", False)
            ):
                data = dict(data)
                data["sessionId"] = session_id
                data["sessionFile"] = session_file
            _emit(
                {
                    "type": "response",
                    "command": "get_session_stats",
                    "success": step.get("success", True),
                    "data": data,
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
