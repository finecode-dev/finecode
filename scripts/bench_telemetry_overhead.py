"""M5b: telemetry overhead and reporting-volume measurement (issue #109).

Compares the working tree ("branch") against a base worktree ("base", sources
put first on PYTHONPATH) for the workloads and ceilings of the plan's P10.4
table. Every row is fixed by the plan; this file is only harness code.

Usage (run with the dev_workspace interpreter):
    python scripts/bench_telemetry_overhead.py \
        --record issues/109-.../records/m5b-<date>.json \
        --only transport --mode off --base /tmp/finecode-base

Rows: transport, startup, phase, rss, idle, reporting, retained.
Modes: off, on-sink, on-closed. The sink is always a separate process.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RECORD_SCHEMA_VERSION = 1


def _host_facts() -> dict:
    facts: dict = {
        "cpu_model": "unknown",
        "cpu_count": os.cpu_count(),
        "kernel": platform.release(),
        "python": sys.version.split()[0],
        "python_build": platform.python_build()[1],
    }
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("model name"):
                facts["cpu_model"] = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    return facts


def load_record(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "schema": RECORD_SCHEMA_VERSION,
        "host": _host_facts(),
        "rows": [],
    }


def save_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")


def record_row(record: dict, row: dict) -> None:
    record["rows"] = [r for r in record["rows"] if r["id"] != row["id"]]
    record["rows"].append(row)


def _env_for(base: str) -> dict[str, str]:
    env = dict(os.environ)
    if base:
        srcs = [
            f"{base}/src",
            f"{base}/finecode_jsonrpc/src",
            f"{base}/finecode_extension_runner/src",
            f"{base}/finecode_extension_api/src",
            f"{base}/extensions/fine_envs",
        ]
        env["PYTHONPATH"] = ":".join(srcs) + (
            f":{env['PYTHONPATH']}" if env.get("PYTHONPATH") else ""
        )
    return env


def _send(sock: socket.socket, method: str, params: dict, req_id: int) -> None:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
    ).encode()
    sock.sendall(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)


def _read(sock: socket.socket, expect_id: int, timeout: float = 120.0) -> dict:
    sock.settimeout(timeout)
    while True:
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = sock.recv(1)
            if not chunk:
                raise EOFError("closed")
            raw += chunk
        head, rest = raw.split(b"\r\n\r\n", 1)
        length = int(head.split(b"Content-Length: ")[1])
        body = rest
        while len(body) < length:
            chunk = sock.recv(length - len(body))
            if not chunk:
                raise EOFError("closed")
            body += chunk
        message = json.loads(body)
        if message.get("id") == expect_id:
            return message


def _wait_port(host: str, port: int, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def start_sink(tmp: Path) -> tuple[subprocess.Popen, str]:
    """Start the OTLP sink as a separate process; return (proc, address)."""
    out = tmp / "sink-out.jsonl"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tests.e2e.otlp_sink",
            "--port",
            "0",
            "--out",
            str(out),
        ],
        cwd=str(REPO),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    assert proc.stdout is not None
    address = ""
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if "OTLP sink on " in line:
            address = line.split("OTLP sink on ")[1].strip()
            break
    assert address, "sink never printed its address"
    return proc, address


def stop_sink(proc: subprocess.Popen) -> None:
    import signal as _signal

    proc.send_signal(_signal.SIGTERM)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=15)


def make_workspace(tmp: Path) -> Path:
    """A workspace with one runnable render action, on a copied venv."""
    import shutil as _shutil

    ws = tmp / "bench-ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "pyproject.toml").write_text(
        "[project]\n"
        'name = "bench-project"\n'
        'version = "0.1.0"\n'
        "\n"
        "[dependency-groups]\n"
        'dev_workspace = ["finecode"]\n'
        "\n"
        "[tool.finecode]\n",
        encoding="utf-8",
    )
    venvs = ws / ".venvs"
    venvs.mkdir(parents=True, exist_ok=True)
    current = Path(sys.executable).parent.parent
    new_venv = venvs / "dev_workspace"
    if not new_venv.exists():
        _shutil.copytree(current, new_venv, symlinks=True)
    activate_path = new_venv / "bin" / "activate"
    if activate_path.exists():
        activate_path.write_text(
            activate_path.read_text(encoding="utf-8").replace(
                str(current), str(new_venv)
            ),
            encoding="utf-8",
        )
    return ws


def start_wm(
    workspace: Path, tmp: Path, env: dict[str, str], tag: str
) -> tuple[subprocess.Popen, int]:
    port_file = tmp / f"wm-port-{tag}"
    if port_file.exists():
        port_file.unlink()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "finecode",
            "start-wm-server",
            "--port-file",
            str(port_file),
            "--disconnect-timeout",
            "10",
        ],
        cwd=str(workspace),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=env,
    )
    deadline = time.monotonic() + 90.0
    port = 0
    while time.monotonic() < deadline:
        if port_file.exists() and port_file.read_text().strip():
            port = int(port_file.read_text().strip())
            break
        time.sleep(0.5)
    assert port, "WM never wrote its port file"
    assert _wait_port("127.0.0.1", port), "WM never accepted connections"
    return proc, port


def stop_wm(proc: subprocess.Popen) -> None:
    import signal as _signal

    try:
        proc.send_signal(_signal.SIGINT)
        proc.wait(timeout=30)
    except Exception:  # noqa: BLE001 - best-effort shutdown
        try:
            proc.kill()
            proc.wait(timeout=30)
        except Exception:  # noqa: BLE001 - best-effort shutdown
            pass


def vm_hwm_kb(pid: int) -> int | None:
    try:
        for line in (
            Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines()
        ):
            if line.startswith("VmHWM:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def thread_cpu_ns(pid: int, tid: int | None = None) -> int | None:
    """Main-thread (or given tid) CPU time in nanoseconds via /proc."""
    import os as _os

    tid = tid if tid is not None else pid
    try:
        parts = (
            Path(f"/proc/{pid}/task/{tid}/stat")
            .read_text(encoding="utf-8")
            .rsplit(")", 1)[1]
            .split()
        )
        utime, stime = int(parts[11]), int(parts[12])
    except (OSError, ValueError, IndexError):
        return None
    hz = _os.sysconf("SC_CLK_TCK")
    return (utime + stime) * 1_000_000_000 // hz


def wm_env(endpoint: str | None) -> dict[str, str]:
    env = dict(os.environ)
    if endpoint is None:
        env.pop("FINECODE_OTLP_ENDPOINT", None)
    else:
        env["FINECODE_OTLP_ENDPOINT"] = endpoint
    return env


TRANSPORT_HARNESS = r'''
"""Transport-level fan-out matrix: session pair over a socketpair."""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import statistics
import sys
import time
import typing

from finecode_jsonrpc.server_transport import ServerStdioTransport, TcpServerTransport
from finecode_jsonrpc import JsonRpcServerSession


async def _amain() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=["off", "on-sink", "on-closed"])
    parser.add_argument("--sink", default="")
    parser.add_argument("--direction", required=True, choices=["wm-to-er", "er-to-wm"])
    parser.add_argument("--fanout", type=int, required=True)
    parser.add_argument("--requests", type=int, required=True)
    parser.add_argument("--reps", type=int, default=3)
    args = parser.parse_args()

    # Each tree's sessions are wired as its ER wires them: the tree's own
    # JsonRpcTracingHooks always, plus (branch) a hop observer always; with
    # telemetry off the observer's opens return None.
    from pathlib import Path as _Path

    from finecode_extension_runner import er_telemetry

    try:
        from finecode_extension_runner.shared_telemetry.health import HealthTracker
        from finecode_extension_runner.shared_telemetry.rpc_hops import OtelHopObserver
    except ImportError:  # base tree: no hop observer
        OtelHopObserver = None  # type: ignore[assignment]
    observer_a = None
    observer_b = None
    if OtelHopObserver is not None:
        wm_health = HealthTracker(process_role="wm")
        observer_a = OtelHopObserver(role="wm", health=wm_health)
        observer_b = OtelHopObserver(role="er", health=er_telemetry._health)

    if args.mode != "off":
        # on-closed exports to a closed port and fails.
        endpoint = args.sink if args.mode == "on-sink" else "http://127.0.0.1:1"
        if OtelHopObserver is not None:
            from finecode_extension_runner.shared_telemetry.providers import setup_providers

            # Same steps as the branch ER's initialize handler.
            service_name = "finecode.er.bench"
            setup = setup_providers(
                service_name=service_name,
                resource_attrs=er_telemetry._er_resource_attrs(service_name, _Path.cwd()),
                endpoint=endpoint,
                health=er_telemetry._health,
            )
            if setup.traces != "active":
                raise SystemExit(f"traces not active: {setup}")
            # The provider is process-wide; the WM side's own setup records the
            # same outcome on its tracker.
            wm_health.set_availability("traces", setup.traces)
            er_telemetry._telemetry_initialized = True
        else:
            # Same call as the base ER's telemetry config handling.
            er_telemetry.apply_telemetry_config(
                {"otlp_endpoint": endpoint}, _Path.cwd(), env_name="bench"
            )
            if not er_telemetry._telemetry_initialized:
                raise SystemExit("base telemetry not initialized")

    def _make_session(observer):
        import inspect

        kwargs: dict = {"tracing": er_telemetry.JsonRpcTracingHooks()}
        if "hop_observer" in inspect.signature(JsonRpcServerSession.__init__).parameters:
            kwargs["hop_observer"] = observer
        return JsonRpcServerSession(**kwargs)

    a_sock, b_sock = socket.socketpair()
    a_reader, a_writer = await asyncio.open_connection(sock=a_sock)
    b_reader, b_writer = await asyncio.open_connection(sock=b_sock)

    transport_a = TcpServerTransport(a_reader, a_writer, readable_id="a")
    transport_b = TcpServerTransport(b_reader, b_writer, readable_id="b")
    session_a = _make_session(observer_a)
    session_b = _make_session(observer_b)
    session_a.attach(transport_a)
    session_b.attach(transport_b)
    await transport_a.start()
    await transport_b.start()

    async def _echo(params: typing.Any) -> typing.Any:
        return {"echo": params}

    session_a.on_request("echo", _echo)
    session_b.on_request("echo", _echo)

    sender, _other = (session_a, session_b) if args.direction == "wm-to-er" else (session_b, session_a)

    async def _one(i: int) -> float:
        started = time.perf_counter_ns()
        result = await sender.send_request("echo", {"i": i}, timeout=60.0)
        assert result == {"echo": {"i": i}}
        return time.perf_counter_ns() - started

    import statistics as _statistics

    per_req: list[float] = []
    cpu_req: list[float] = []
    latencies: list[float] = []
    for _ in range(args.reps):
        rep_lat: list[float] = []
        wall_start = time.monotonic()
        cpu_start = time.thread_time_ns()
        remaining = args.requests
        while remaining > 0:
            batch = min(args.fanout, remaining)
            rep_lat.extend(await asyncio.gather(*[_one(i) for i in range(batch)]))
            remaining -= batch
        wall_s = time.monotonic() - wall_start
        cpu_s = (time.thread_time_ns() - cpu_start) / 1e9
        per_req.append(wall_s * 1e6 / args.requests)
        cpu_req.append(cpu_s * 1e6 / args.requests)
        latencies.extend(rep_lat)

    lat_sorted = sorted(latencies)
    p95 = lat_sorted[max(0, int(len(lat_sorted) * 0.95) - 1)] / 1000.0
    vm_hwm_kb = None
    with open("/proc/self/status", encoding="utf-8") as status:
        for line in status:
            if line.startswith("VmHWM:"):
                vm_hwm_kb = int(line.split()[1])
                break
    print(json.dumps({
        "per_request_us": _statistics.median(per_req),
        "cpu_per_request_us": _statistics.median(cpu_req),
        "p95_us": p95,
        "vm_hwm_kb": vm_hwm_kb,
    }))

    await transport_a.stop()
    await transport_b.stop()


asyncio.run(_amain())
'''


def run_transport_matrix(
    record: dict, base: str, mode: str, sink_addr: str, tmp: Path
) -> None:
    """Row 1 (+p95 row 2): amortized elapsed and main-thread CPU per request."""
    harness = tmp / "transport_harness.py"
    harness.write_text(TRANSPORT_HARNESS, encoding="utf-8")
    for direction in ("wm-to-er", "er-to-wm"):
        for fanout in (1, 16, 128):
            for label, extra_env in (
                ("base", _env_for(base)),
                ("branch", _env_for("")),
            ):
                if mode == "on-sink" and not sink_addr:
                    raise AssertionError("on-sink needs a sink address")
                cmd = [
                    sys.executable,
                    str(harness),
                    "--mode",
                    mode,
                    "--direction",
                    direction,
                    "--fanout",
                    str(fanout),
                    "--requests",
                    "2000",
                ]
                if mode == "on-sink":
                    cmd += ["--sink", f"http://{sink_addr}"]
                env = dict(extra_env)
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=600, env=env
                )
                if proc.returncode != 0:
                    raise AssertionError(
                        f"transport harness failed: {proc.stderr[-2000:]}"
                    )
                sample = json.loads(proc.stdout.strip().splitlines()[-1])
                record_row(
                    record,
                    {
                        "id": f"transport/{direction}/fanout-{fanout}/{mode}/{label}",
                        "per_request_us": sample["per_request_us"],
                        "cpu_per_request_us": sample["cpu_per_request_us"],
                        "p95_us": sample["p95_us"],
                        "vm_hwm_kb": sample.get("vm_hwm_kb"),
                    },
                )


def check_rss_ceiling(rows: list[dict], mode: str) -> list[str]:
    """Row 5: peak RSS (VmHWM) rise in the fan-out 128 run, per direction.

    The transport harness holds both session ends in one process, so the
    measured process carries the WM-side and ER-side hop state together.
    """
    failures: list[str] = []
    by_id = {r["id"]: r for r in rows}
    allowed_kb = 1024 if mode == "off" else 32 * 1024
    for direction in ("wm-to-er", "er-to-wm"):
        key = f"transport/{direction}/fanout-128/{mode}"
        base = by_id.get(f"{key}/base", {}).get("vm_hwm_kb")
        branch = by_id.get(f"{key}/branch", {}).get("vm_hwm_kb")
        if base is None or branch is None:
            failures.append(f"rss {mode} {direction}: VmHWM missing")
            continue
        rise = branch - base
        if rise > allowed_kb:
            failures.append(
                f"rss {mode} {direction}: rise {rise} KiB > {allowed_kb} KiB"
            )
    return failures


def check_transport_ceiling(rows: list[dict], mode: str) -> list[str]:
    """Evaluate the row-1/row-2 ceilings for one mode; return failure strings."""
    failures: list[str] = []
    by_id = {r["id"]: r for r in rows}
    for direction in ("wm-to-er", "er-to-wm"):
        for fanout in (1, 16, 128):
            key = f"transport/{direction}/fanout-{fanout}/{mode}"
            if f"{key}/base" not in by_id or f"{key}/branch" not in by_id:
                continue
            base = by_id[f"{key}/base"]["per_request_us"]
            branch = by_id[f"{key}/branch"]["per_request_us"]
            rise = branch - base
            if mode == "off":
                allowed = max(10.0, 0.10 * base)
            else:
                allowed = 400.0
            if rise > allowed:
                failures.append(
                    f"{mode} {direction} fanout {fanout}: rise {rise:.1f}us > {allowed:.1f}us"
                )
        if mode == "on-sink":
            key = f"transport/{direction}/fanout-1/on-sink/branch"
            if key in by_id and by_id[key]["p95_us"] > 600.0:
                failures.append(
                    f"p95 {direction}: {by_id[key]['p95_us']:.1f}us > 600us"
                )
    return failures


def run_startup_row(
    record: dict, base: str, mode: str, sink_addr: str, tmp: Path
) -> None:
    """Row 3: ER spawn-to-RUNNING p95 over 30 starts (real WM, polled)."""
    workspace = make_workspace(tmp / f"ws-startup-{mode}")
    endpoint = {
        "off": None,
        "on-sink": f"http://{sink_addr}",
        "on-closed": "http://127.0.0.1:1",
    }[mode]
    for label, env in (("base", _env_for(base)), ("branch", _env_for(""))):
        latencies_ms: list[float] = []
        wm_proc_env = wm_env(endpoint)
        if "PYTHONPATH" in env:
            wm_proc_env["PYTHONPATH"] = env["PYTHONPATH"]
        proc, port = start_wm(
            workspace, tmp, wm_proc_env, f"startup-{mode}-{label}"
        )
        try:
            with socket.create_connection(("127.0.0.1", port)) as sock:
                _send(sock, "workspace/addDir", {"dirPath": str(workspace)}, 1)
                _read(sock, expect_id=1, timeout=180.0)
                for i in range(30):
                    started = time.monotonic()
                    _send(sock, "runners/restart", {"project": str(workspace)}, 100 + i)
                    reply = _read(sock, expect_id=100 + i, timeout=180.0)
                    assert "result" in reply, reply
                    latencies_ms.append((time.monotonic() - started) * 1000.0)
        finally:
            stop_wm(proc)
        ordered = sorted(latencies_ms)
        p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)]
        record_row(
            record,
            {
                "id": f"startup/{mode}/{label}",
                "p95_ms": p95,
                "samples_ms": latencies_ms,
            },
        )


def check_startup_ceiling(rows: list[dict], mode: str) -> list[str]:
    failures: list[str] = []
    by_id = {r["id"]: r for r in rows}
    key = f"startup/{mode}"
    if f"{key}/base" not in by_id or f"{key}/branch" not in by_id:
        return []
    base = by_id[f"{key}/base"]["p95_ms"]
    branch = by_id[f"{key}/branch"]["p95_ms"]
    allowed = max(10.0, 0.05 * base) if mode == "off" else 250.0
    if branch - base > allowed:
        failures.append(f"startup {mode}: rise {branch - base:.1f}ms > {allowed:.1f}ms")
    return failures


PHASE_HARNESS = r"""
# Synchronous-phase render cost: interleaved raw vs scoped renders.

from __future__ import annotations

import argparse
import json
import time

parser = argparse.ArgumentParser()
parser.add_argument("--mode", required=True, choices=["absent", "unbound", "bound"])
args = parser.parse_args()

from fine_envs.dump_config_render import render_config_dump

raw = {"tool": {"finecode": {"a": list(range(200))}}, "project": {"name": "p"}, "x": list(range(200))}
selection = {"dep-a": ["e1", "e2"], "dep-b": ["e3"]}

if args.mode in ("unbound", "bound"):
    from finecode_extension_api.phase_timing import synchronous_phase
    from finecode_extension_api.phase_timing_runtime import set_phase_timing_binding
    if args.mode == "bound":
        from finecode_extension_runner.shared_telemetry.health import HealthTracker
        from finecode_extension_runner.shared_telemetry.phase_spans import OtelPhaseTimingBinding

        set_phase_timing_binding(OtelPhaseTimingBinding(health=HealthTracker(process_role="er")))


def _render_scoped():
    with synchronous_phase(
        "render_config_dump",
        input_counts=lambda: {
            "top_level_keys": len(raw),
            "selected_dependencies": len(selection),
            "selected_extras": sum(len(v) for v in selection.values()),
        },
    ) as phase:
        content = render_config_dump(raw, selection)
        phase.completion_counts(lambda: {"output_chars": len(content)})
    return content


import random as _random

calls = 2000
pairs = []
cpu_pairs = []
raws = []
for _ in range(100):
    render_config_dump(raw, selection)
    if args.mode != "absent":
        _render_scoped()
for _ in range(calls):
    scoped_first = _random.random() < 0.5
    def _raw_once():
        start = time.perf_counter_ns()
        cpu_start = time.thread_time_ns()
        render_config_dump(raw, selection)
        return time.perf_counter_ns() - start, time.thread_time_ns() - cpu_start

    def _scoped_once():
        start = time.perf_counter_ns()
        cpu_start = time.thread_time_ns()
        _render_scoped()
        return time.perf_counter_ns() - start, time.thread_time_ns() - cpu_start

    if args.mode == "absent":
        raw_ns, raw_cpu = _raw_once()
        scoped_ns, scoped_cpu = _raw_once()
    elif not scoped_first:
        raw_ns, raw_cpu = _raw_once()
        scoped_ns, scoped_cpu = _scoped_once()
    else:
        scoped_ns, scoped_cpu = _scoped_once()
        raw_ns, raw_cpu = _raw_once()
    pairs.append((scoped_ns - raw_ns) / 1000.0)
    cpu_pairs.append((scoped_cpu - raw_cpu) / 1000.0)
    raws.append(raw_ns / 1000.0)
pairs.sort()
cpu_pairs.sort()
raws.sort()
n = len(pairs)
print(json.dumps({
    "per_call_us": pairs[n // 2],
    "cpu_per_call_us": cpu_pairs[n // 2],
    "raw_us": raws[n // 2],
    "p10_us": pairs[n // 10],
    "p90_us": pairs[n * 9 // 10],
}))
"""


def run_phase_row(record: dict, base: str, tmp: Path) -> None:
    """Row 4: config render cost, interleaved raw vs scoped (median paired diff)."""
    harness = tmp / "phase_harness.py"
    harness.write_text(PHASE_HARNESS, encoding="utf-8")
    results: dict[str, dict] = {}
    for label, extra_env in (("base", _env_for(base)), ("branch", _env_for(""))):
        for mode in ["absent"] if label == "base" else ["unbound", "bound"]:
            cmd = [sys.executable, str(harness), "--mode", mode]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=600, env=dict(extra_env)
            )
            if proc.returncode != 0:
                if label == "base" and "phase_timing" in (proc.stderr or ""):
                    results[f"{label}/{mode}"] = {"note": "no phase API at base"}
                    continue
                raise AssertionError(f"phase harness failed: {proc.stderr[-2000:]}")
            sample = json.loads(proc.stdout.strip().splitlines()[-1])
            results[f"{label}/{mode}"] = sample
            record_row(
                record,
                {
                    "id": f"phase/{label}/{mode}",
                    "per_call_us": sample["per_call_us"],
                    "cpu_per_call_us": sample["cpu_per_call_us"],
                    "raw_us": sample.get("raw_us"),
                    "p10_us": sample.get("p10_us"),
                    "p90_us": sample.get("p90_us"),
                },
            )
    failures: list[str] = []
    unbound = results["branch/unbound"]["per_call_us"]
    if unbound > 20.0:
        failures.append(f"phase unbound: overhead {unbound:.1f}us > 20us")
    raw = results["branch/bound"]["raw_us"] or 0.0
    bound = results["branch/bound"]["per_call_us"]
    allowed = max(5000.0, 0.02 * raw)
    if bound > allowed:
        failures.append(f"phase bound: overhead {bound:.1f}us > {allowed:.1f}us")
    record_row(record, {"id": "phase/ceiling-check", "failures": failures})


IDLE_HARNESS = r"""
# Idle endpoint-enabled ER: real initialize against the endpoint, then idle.
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--endpoint", required=True)
args = parser.parse_args()

from finecode_extension_runner import er_server
from finecode_extension_runner import global_state
from finecode_jsonrpc.server_transport import ServerStdioTransport

global_state.env_name = "idle"
global_state.project_dir_path = Path("/tmp/idle-proj")


async def _amain() -> None:
    server = er_server.create_er_server()
    transport = ServerStdioTransport()
    server._session.attach(transport)
    reader = asyncio.StreamReader()
    read_task = asyncio.ensure_future(transport._read_messages(reader))
    await er_server._on_initialize(
        server,
        {"initializationOptions": {"telemetry": {"mode": "otlp", "otlpEndpoint": args.endpoint}}},
    )
    await asyncio.sleep(120.0)
    reader.feed_eof()
    await asyncio.gather(read_task, return_exceptions=True)
    await transport.stop()
    monitor = server._lag_monitor
    if monitor is not None and monitor._task is not None:
        await monitor.stop()


asyncio.run(_amain())
print(json.dumps({"ok": True}))
"""


def run_idle_row(record: dict, base: str, sink_addr: str, tmp: Path) -> None:
    """Row 6: 120 s idle endpoint-enabled ER stays under 1% of one core."""
    import psutil

    harness = tmp / "idle_harness.py"
    harness.write_text(IDLE_HARNESS, encoding="utf-8")
    for label, extra_env in (("base", _env_for(base)), ("branch", _env_for(""))):
        env = dict(extra_env)
        proc = subprocess.Popen(
            [sys.executable, str(harness), "--endpoint", f"http://{sink_addr}"],
            env=env,
            stdout=subprocess.DEVNULL,
        )
        try:
            target = psutil.Process(proc.pid)
            target.cpu_percent(interval=None)
            time.sleep(120.0)
            # psutil reports percent of one core; the ceiling is "% of one core".
            pct = target.cpu_percent(interval=None)
            peak_rss = target.memory_info().rss
        finally:
            proc.terminate()
            proc.wait(timeout=30)
        record_row(
            record,
            {
                "id": f"idle/{label}",
                "cpu_percent_of_core": pct,
                "peak_rss_bytes": peak_rss,
            },
        )
    branch_pct = next(r for r in record["rows"] if r["id"] == "idle/branch")[
        "cpu_percent_of_core"
    ]
    record_row(
        record,
        {
            "id": "idle/ceiling-check",
            "failures": (
                [] if branch_pct <= 1.0 else [f"idle cpu {branch_pct:.2f}% > 1%"]
            ),
        },
    )


REPORTING_HARNESS = r'''
"""Reporting-volume probes: lag episode, late-response flood, churn."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import typing
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--probe", required=True, choices=["lag65", "late1000", "churn5"])
args = parser.parse_args()

from finecode_extension_runner import er_server, er_telemetry, global_state
from finecode_extension_runner.shared_telemetry.health import HealthTracker
from finecode_jsonrpc.server_transport import ServerStdioTransport

global_state.env_name = "reporting"
global_state.project_dir_path = Path("/tmp/reporting-proj")


async def _lag65() -> dict:
    import unittest.mock as mock

    from loguru import logger as _logger

    server = er_server.create_er_server()
    transport = ServerStdioTransport()
    server._session.attach(transport)
    written: list[bytes] = []

    class _W:
        def write(self, data: bytes) -> None:
            written.append(data)

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

    wt = asyncio.ensure_future(
        transport._write_messages(typing.cast(typing.Any, _W()))
    )
    reader = asyncio.StreamReader()
    rt = asyncio.ensure_future(transport._read_messages(reader))

    def _frame(message: dict) -> bytes:
        body = json.dumps(message).encode()
        return f"Content-Length: {len(body)}\r\n\r\n".encode() + body

    async def _req(mid: int, method: str, params: dict) -> dict:
        reader.feed_data(
            _frame({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        )
        for _ in range(2000):
            for data in written:
                _h, _, pl = data.decode().partition("\r\n\r\n")
                reply = json.loads(pl)
                if reply.get("id") == mid:
                    return reply
            await asyncio.sleep(0)
        raise AssertionError(mid)

    records: list = []
    sink_id = _logger.add(lambda m: records.append(m.record))
    try:
        reply = await _req(
            1,
            "initialize",
            {"initializationOptions": {"telemetry": {"mode": "otlp", "otlpEndpoint": "http://127.0.0.1:1"}}},
        )
        assert reply["result"]["telemetry"]["traces"] == "active"
        assert server._lag_monitor is not None
        for _ in range(38):
            time.sleep(1.6)
            await asyncio.sleep(0.1)
        await asyncio.sleep(2.0)
        await server._lag_monitor.stop()
    finally:
        _logger.remove(sink_id)
    warnings = [
        r for r in records if str(r["message"]).startswith("ER event loop lag exceeded")
    ]
    recoveries = [
        r for r in records if str(r["message"]).startswith("ER event loop lag recovered")
    ]
    counts = er_telemetry._health.snapshot()["signals"]["lag_monitor"]["counts"]
    reader.feed_eof()
    await asyncio.gather(rt, return_exceptions=True)
    wt.cancel()
    await asyncio.gather(wt, return_exceptions=True)
    await transport.stop()
    return {
        "warnings": len(warnings),
        "recoveries": len(recoveries),
        "suppressed": counts["suppressed"],
    }


async def _late1000() -> dict:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry import trace as otel_trace

    from finecode_extension_runner.shared_telemetry.rpc_hops import OtelHopObserver
    from finecode_jsonrpc.tracing import HopIdentity, capture_stage_time

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    otel_trace.set_tracer_provider(provider)

    health = HealthTracker(process_role="er")
    health.set_availability("traces", "active")
    observer = OtelHopObserver(role="er", health=health)
    import logging as _logging

    records: list = []
    from loguru import logger as _logger

    sink_id = _logger.add(lambda m: records.append(m.record))
    started = time.monotonic()
    try:
        for i in range(1000):
            token = observer.open_caller(
                HopIdentity(connection_id="c", role="caller", request_id=i, method="m"),
                "peer",
                capture_stage_time(),
            )
            assert token is not None
            observer.close_caller(token, "timeout", capture_stage_time())
            observer.response_without_waiter(
                HopIdentity(connection_id="c", role="caller", request_id=i, method="m"),
                capture_stage_time(),
            )
    finally:
        _logger.remove(sink_id)
    elapsed = time.monotonic() - started
    late_logs = sum(
        1 for r in records if "late_response" in str(r["message"])
    )
    counts = health.snapshot()["signals"]["traces"]["counts"]
    return {"elapsed_s": elapsed, "late_logs": late_logs, "suppressed": counts["suppressed"]}


async def _churn5() -> dict:
    records: list = []
    from loguru import logger as _logger

    sink_id = _logger.add(lambda m: records.append(m.record))
    try:
        for _ in range(5):
            server = er_server.create_er_server()
            await er_server._on_shutdown(server, None)
    finally:
        _logger.remove(sink_id)
    warnings = [r for r in records if r["level"].name == "WARNING"]
    return {"warnings": len(warnings)}


async def _amain() -> None:
    if args.probe == "lag65":
        result = await _lag65()
    elif args.probe == "late1000":
        result = await _late1000()
    else:
        result = await _churn5()
    print(json.dumps(result))


asyncio.run(_amain())
'''


def _log_files_since(log_dir: Path, pattern: str, since: float) -> list[Path]:
    """Log files rotated in by runs started after `since`."""
    out = []
    for path in sorted(log_dir.glob(pattern)):
        try:
            if path.stat().st_mtime >= since:
                out.append(path)
        except OSError:
            continue
    return out


def _wm_log_files_since(since: float) -> list[Path]:
    """WM server log files rotated in by runs started after `since`."""
    return _log_files_since(
        REPO / ".venvs" / "dev_workspace" / "logs" / "wm_server",
        "wm_server_*.log",
        since,
    )


def _count_levels(paths: list[Path]) -> dict[str, int]:
    """Categorized log counts across log files."""
    counts = {"warnings": 0, "info_summaries": 0, "lag_warnings": 0, "late_logs": 0}
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if "| WARNING  |" in line or "| WARNING |" in line:
                counts["warnings"] += 1
            if "telemetry health summary" in line and "| INFO " in line:
                counts["info_summaries"] += 1
            if "event loop lag exceeded threshold" in line:
                counts["lag_warnings"] += 1
            if "late RPC telemetry record" in line:
                counts["late_logs"] += 1
    return counts


def _wait_er_child(wm_proc: subprocess.Popen, timeout: float) -> int:
    """Wait for a running ER child of the WM; return its pid."""
    import psutil

    try:
        wm = psutil.Process(wm_proc.pid)
    except Exception as exc:  # noqa: BLE001 - harness helper
        raise AssertionError(f"WM process gone: {exc}") from exc
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            children = wm.children(recursive=True)
        except Exception:  # noqa: BLE001 - process may be exiting
            children = []
        for child in children:
            try:
                cmdline = " ".join(child.cmdline())
            except Exception:  # noqa: BLE001 - process may be exiting
                continue
            if "finecode_extension_runner" in cmdline:
                return child.pid
        time.sleep(1.0)
    raise AssertionError("no ER child appeared")


def run_reporting_row(record: dict, base: str, sink_addr: str, tmp: Path) -> None:
    """Row 7: reporting volume bounds across healthy/churn/unreachable/lag/late."""
    harness = tmp / "reporting_harness.py"
    harness.write_text(REPORTING_HARNESS, encoding="utf-8")
    failures: list[str] = []

    def _run_harness(probe: str, timeout: int) -> dict:
        proc = subprocess.run(
            [sys.executable, str(harness), "--probe", probe],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_env_for(""),
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"reporting harness {probe} failed: {proc.stderr[-2000:]}"
            )
        return json.loads(proc.stdout.strip().splitlines()[-1])

    lag = _run_harness("lag65", 300)
    record_row(record, {"id": "reporting/lag65", **lag})
    if not 2 <= lag["warnings"] <= 4:
        failures.append(f"65s lag produced {lag['warnings']} warnings, want 2-4")
    if lag["recoveries"] != 1:
        failures.append(f"65s lag produced {lag['recoveries']} recoveries, want 1")
    if lag["suppressed"] < 30:
        failures.append(f"65s lag suppressed only {lag['suppressed']} warnings")
    late = _run_harness("late1000", 300)
    record_row(record, {"id": "reporting/late1000", **late})
    allowed_late = 20 + late["elapsed_s"] + 2.0
    if late["late_logs"] > allowed_late:
        failures.append(
            f"late logs {late['late_logs']} > 20+1/s over {late['elapsed_s']:.1f}s"
        )
    if late["suppressed"] < 1:
        failures.append("late flood counted no S7 suppression")
    churn = _run_harness("churn5", 300)
    record_row(record, {"id": "reporting/churn5", **churn})
    if churn["warnings"] != 0:
        failures.append(f"churn produced {churn['warnings']} warnings")

    workspace = make_workspace(tmp / "ws-reporting")
    healthy_mark = time.time()
    proc, port = start_wm(
        workspace, tmp, wm_env(f"http://{sink_addr}"), "reporting-healthy"
    )
    try:
        with socket.create_connection(("127.0.0.1", port)) as sock:
            _send(sock, "workspace/addDir", {"dirPath": str(workspace)}, 1)
            _read(sock, expect_id=1, timeout=180.0)
            _wait_er_child(proc, timeout=120.0)
            time.sleep(600.0)
    finally:
        stop_wm(proc)
    wm_healthy_logs = _wm_log_files_since(healthy_mark)
    assert wm_healthy_logs, "healthy run wrote no WM log"
    er_log_dir = workspace / ".venvs" / "dev_workspace" / "logs" / "runner"
    er_healthy_logs = _log_files_since(er_log_dir, "*.log", healthy_mark)
    assert er_healthy_logs, "healthy run wrote no ER log"
    wm_counts = _count_levels(wm_healthy_logs)
    er_counts = _count_levels(er_healthy_logs)
    record_row(
        record,
        {
            "id": "reporting/healthy-10min",
            "wm": wm_counts,
            "er": er_counts,
        },
    )
    if wm_counts["warnings"] != 0:
        failures.append(f"healthy run WM produced {wm_counts['warnings']} warnings")
    if wm_counts["lag_warnings"] != 0 or er_counts["lag_warnings"] != 0:
        failures.append("healthy run produced lag warnings")
    if wm_counts["late_logs"] != 0 or er_counts["late_logs"] != 0:
        failures.append("healthy run produced late-stage logs")
    if er_counts["warnings"] != 0:
        failures.append(f"healthy run ER produced {er_counts['warnings']} warnings")
    for name, count in (
        ("WM", wm_counts["info_summaries"]),
        ("ER", er_counts["info_summaries"]),
    ):
        if count > 600.0 / 60.0 + 2:
            failures.append(f"healthy run {name} emitted {count} INFO summaries")

    unreachable_mark = time.time()
    proc, port = start_wm(
        workspace, tmp, wm_env("http://127.0.0.1:1"), "reporting-unreachable"
    )
    try:
        with socket.create_connection(("127.0.0.1", port)) as sock:
            _send(sock, "workspace/addDir", {"dirPath": str(workspace)}, 1)
            _read(sock, expect_id=1, timeout=180.0)
            time.sleep(45.0)
    finally:
        stop_wm(proc)
    unreachable_logs = _wm_log_files_since(unreachable_mark)
    assert unreachable_logs, "unreachable run wrote no WM log"
    reachability = 0
    for path in unreachable_logs:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        reachability += sum(
            1 for line in text.splitlines() if "not reachable yet" in line
        )
    record_row(
        record, {"id": "reporting/unreachable", "reachability_warnings": reachability}
    )
    if reachability != 1:
        failures.append(
            f"unreachable collector produced {reachability} reachability warnings, want 1"
        )

    record_row(record, {"id": "reporting/ceiling-check", "failures": failures})


RETAINED_HARNESS = r'''
"""Retained-state caps: hop cap, long-hop expiry, late-entry flood."""

from __future__ import annotations

import asyncio
import json
import time

from finecode_extension_runner.shared_telemetry.health import HealthTracker
from finecode_extension_runner.shared_telemetry.rpc_hops import OtelHopObserver
from finecode_jsonrpc.tracing import HopIdentity, capture_stage_time


async def _amain() -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry import trace as otel_trace

    otel_trace.set_tracer_provider(TracerProvider())
    health = HealthTracker(process_role="er")
    health.set_availability("traces", "active")
    observer = OtelHopObserver(role="er", health=health)
    tokens = []
    for i in range(2000):
        tokens.append(
            observer.open_caller(
                HopIdentity(connection_id="c", role="caller", request_id=i, method="m"),
                "peer",
                capture_stage_time(),
            )
        )
    assert tokens[1024] is None
    assert all(t is not None for t in tokens[:1024])
    assert all(t is None for t in tokens[1024:])
    assert len(observer._open) <= 1024
    counts = health.snapshot()["signals"]["traces"]["counts"]
    assert counts["suppressed"] >= 976

    for i in range(3000):
        observer.response_without_waiter(
            HopIdentity(connection_id="c", role="caller", request_id=i, method="m"),
            capture_stage_time(),
        )
    assert len(observer._late) <= 1024

    print(json.dumps({"ok": True, "suppressed": counts["suppressed"]}))


asyncio.run(_amain())
'''


def run_retained_row(record: dict, base: str, tmp: Path) -> None:
    """Row 8: retained-state caps (branch-only; the caps are new in D7)."""
    harness = tmp / "retained_harness.py"
    harness.write_text(RETAINED_HARNESS, encoding="utf-8")
    env = _env_for("")
    proc = subprocess.run(
        [sys.executable, str(harness)],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )
    if proc.returncode != 0:
        raise AssertionError(f"retained harness failed: {proc.stderr[-2000:]}")
    sample = json.loads(proc.stdout.strip().splitlines()[-1])
    record_row(record, {"id": "retained/caps", **sample, "failures": []})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--record", required=True)
    parser.add_argument(
        "--only",
        default=",".join(
            ["transport", "startup", "phase", "idle", "reporting", "retained"]
        ),
    )
    parser.add_argument(
        "--mode", default="off", choices=["off", "on-sink", "on-closed"]
    )
    parser.add_argument("--base", default="")
    parser.add_argument("--work", default="")
    args = parser.parse_args()

    record = load_record(Path(args.record))
    tmp = Path(args.work or tempfile.mkdtemp(prefix="bench-m5b-"))
    tmp.mkdir(parents=True, exist_ok=True)
    only = set(args.only.split(","))
    sink_proc = None
    sink_addr = ""
    if args.mode == "on-sink" or "idle" in only or "reporting" in only:
        sink_proc, sink_addr = start_sink(tmp)
    try:
        if "transport" in only:
            run_transport_matrix(record, args.base, args.mode, sink_addr, tmp)
            save_record(Path(args.record), record)
            record_row(
                record,
                {
                    "id": f"transport/ceiling-check/{args.mode}",
                    "failures": check_transport_ceiling(record["rows"], args.mode),
                }
            )
            record_row(
                record,
                {
                    "id": f"rss/ceiling-check/{args.mode}",
                    "failures": check_rss_ceiling(record["rows"], args.mode),
                },
            )
            save_record(Path(args.record), record)
        if "startup" in only:
            run_startup_row(record, args.base, args.mode, sink_addr, tmp)
            save_record(Path(args.record), record)
            record_row(
                record,
                {
                    "id": f"startup/ceiling-check/{args.mode}",
                    "failures": check_startup_ceiling(record["rows"], args.mode),
                }
            )
            save_record(Path(args.record), record)
        if "phase" in only:
            run_phase_row(record, args.base, tmp)
            save_record(Path(args.record), record)
        if "idle" in only:
            run_idle_row(record, args.base, sink_addr, tmp)
            save_record(Path(args.record), record)
        if "reporting" in only:
            run_reporting_row(record, args.base, sink_addr, tmp)
            save_record(Path(args.record), record)
        if "retained" in only:
            run_retained_row(record, args.base, tmp)
            save_record(Path(args.record), record)
    finally:
        if sink_proc is not None:
            stop_sink(sink_proc)


if __name__ == "__main__":
    main()
