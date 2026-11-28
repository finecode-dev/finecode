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


def _find_session_file(config_dir: str, session_id: str) -> str | None:
    matches: list[str] = []
    projects = os.path.join(config_dir, "projects")
    try:
        entries = os.listdir(projects)
    except OSError:
        return None
    for entry in entries:
        candidate = os.path.join(config_dir, "projects", entry, f"{session_id}.jsonl")
        if not os.path.isfile(candidate) or os.path.islink(candidate):
            continue
        try:
            with open(candidate, encoding="utf-8") as handle:
                content = handle.read()
        except OSError:
            continue
        if f'"sessionId": "{session_id}"' in content:
            matches.append(candidate)
    if len(matches) == 1:
        return matches[0]
    return None


def main() -> int:
    scenario = json.loads(sys.argv[1])
    rest = sys.argv[2:]
    if "--version" in rest:
        sys.stdout.write(str(scenario.get("version", "2.1.306")) + "\n")
        sys.stdout.flush()
        return 0
    resume_store: str | None = None
    resume_id = ""
    if "--resume" in rest:
        resume_id = rest[rest.index("--resume") + 1]
        config_dir = scenario.get("config_dir") or os.environ.get("CLAUDE_CONFIG_DIR")
        if config_dir:
            resume_store = _find_session_file(config_dir, resume_id)
        if scenario.get("record_driver"):
            _record(scenario, {"argv": sys.argv[2:]})
        if resume_store is None:
            sys.stderr.write(f"no such session: {resume_id}\n")
            sys.stderr.flush()
            return 2
        # A resume reopens the stored transcript; it never rewrites it.
        scenario = dict(scenario, write_native=False)
    session_id = str(scenario.get("session_id", _SESSION))
    config_dir = scenario.get("config_dir") or os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir and scenario.get("write_native"):
        try:
            proj = scenario.get("native_proj", "testproj")
            main_path = os.path.join(
                str(config_dir), "projects", proj, f"{session_id}.jsonl"
            )
            os.makedirs(os.path.dirname(main_path), exist_ok=True)
            main_records = scenario.get(
                "native_main",
                [
                    {
                        "type": "user",
                        "sessionId": session_id,
                        "message": {"content": "prompt marker PROMPT-1"},
                    },
                    {
                        "type": "assistant",
                        "sessionId": session_id,
                        "message": {"content": "assistant marker ALPHA"},
                    },
                    {
                        "type": "user",
                        "sessionId": session_id,
                        "message": {"tool": "tool marker BETA"},
                    },
                ],
            )
            with open(main_path, "w", encoding="utf-8") as handle:
                handle.writelines([json.dumps(entry) + "\n" for entry in main_records])
            for related in scenario.get("native_related", []):
                rel_path = os.path.join(
                    str(config_dir),
                    "projects",
                    proj,
                    session_id,
                    related["path"],
                )
                os.makedirs(os.path.dirname(rel_path), exist_ok=True)
                with open(rel_path, "w", encoding="utf-8") as handle:
                    handle.writelines(
                        [
                            json.dumps(entry) + "\n"
                            for entry in related.get("records", [])
                        ]
                    )
        except OSError:
            pass

    if scenario.get("record_driver"):
        # The CLI flags the handler assembled, gated so scenarios that inspect
        # the whole record list keep its shape. Tests read this by key.
        _record(scenario, {"argv": sys.argv[2:]})

    for step in scenario["steps"]:
        action = step["do"]

        if action == "read_prompt":
            # The real CLI reads the prompt from stdin until EOF when no
            # prompt argument is given, which is how the handler sends it.
            prompt_text = sys.stdin.read()
            _record(scenario, {"prompt": prompt_text})
            if resume_store is not None:
                try:
                    with open(resume_store, "a", encoding="utf-8") as handle:
                        _ = handle.write(
                            json.dumps(
                                {
                                    "type": "user",
                                    "sessionId": resume_id,
                                    "message": {"content": prompt_text},
                                }
                            )
                            + "\n"
                        )
                except OSError:
                    pass

        elif action == "read_initialize":
            line = sys.stdin.readline()
            try:
                frame = json.loads(line) if line else None
            except ValueError:
                frame = None
            _record(scenario, {"initialize": frame})
            request_id = None
            if isinstance(frame, dict):
                request_id = frame.get("request_id")
            if not isinstance(request_id, str):
                request_id = "req_1_unknown"
            _emit(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": request_id,
                        "response": {},
                    },
                }
            )

        elif action == "read_user":
            line = sys.stdin.readline()
            try:
                frame = json.loads(line) if line else None
            except ValueError:
                frame = None
            _record(scenario, {"user": frame})
            if resume_store is not None and isinstance(frame, dict):
                try:
                    message = frame.get("message")
                    content = (
                        message.get("content") if isinstance(message, dict) else ""
                    )
                    with open(resume_store, "a", encoding="utf-8") as handle:
                        _ = handle.write(
                            json.dumps(
                                {
                                    "type": "user",
                                    "sessionId": resume_id,
                                    "message": {"content": content},
                                }
                            )
                            + "\n"
                        )
                except OSError:
                    pass

        elif action == "request":
            request: dict[str, object] = {"subtype": "can_use_tool"}
            for key in (
                "tool_name",
                "display_name",
                "input",
                "tool_use_id",
                "description",
                "permission_suggestions",
                "requires_user_interaction",
            ):
                if key in step:
                    request[key] = step[key]
            if "subtype" in step:
                request["subtype"] = step["subtype"]
            frame = {
                "type": "control_request",
                "request_id": step["request_id"],
                "request": request,
            }
            _record(scenario, {"request": frame})
            _emit(frame)

        elif action == "await_response":
            request_id = step["request_id"]
            within = step.get("within")
            deadline = None if within is None else time.time() + float(within)
            while True:
                if deadline is not None and time.time() > deadline:
                    _record(scenario, {"no_response": request_id})
                    break
                line = sys.stdin.readline()
                if not line:
                    _record(scenario, {"no_response": request_id})
                    break
                try:
                    frame = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(frame, dict):
                    continue
                if frame.get("type") != "control_response":
                    continue
                response = frame.get("response")
                if not isinstance(response, dict):
                    continue
                if response.get("request_id") != request_id:
                    continue
                _record(scenario, {"response": frame})
                break

        elif action == "cancel_request":
            _emit({"type": "control_cancel_request", "request_id": step["request_id"]})

        elif action == "await_stdin_eof":
            sys.stdin.read()
            _record(scenario, {"stdin_eof": True})

        elif action == "touch":
            path = step["path"]
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("touched")
            _record(scenario, {"touched": path})

        elif action == "wait_for":
            path = step["path"]
            timeout = float(step.get("timeout", 20))
            deadline = time.time() + timeout
            while not os.path.exists(path):
                if time.time() > deadline:
                    break
                time.sleep(0.05)
            _record(scenario, {"waited": path})

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
                    "session_id": step.get(
                        "session_id", scenario.get("session_id", _SESSION)
                    ),
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
