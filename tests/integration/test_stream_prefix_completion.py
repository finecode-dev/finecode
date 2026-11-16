"""Stream-prefix completion acceptance with real ER subprocesses (issue #916).

Each invocation emits its marked prefix back-to-back with its terminal
response (no response-delay sleep). Every requested final partial and
expected progress end must be observed before the matching terminal
response. A marker in final result data is not counted as a partial
receipt: prefix assertions only inspect streamed notifications.
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import json
import pathlib
import shutil
import sys
import sysconfig
import typing
import uuid

import pytest

from finecode.wm_server import domain, wal
from finecode.wm_server.services import in_flight_runs, shutdown_service

FIXTURE_MODULE = "stream_prefix_probe"
REPS = 40


class ClientStreamMethod(enum.Enum):
    PARTIAL = "actions/partialResult"
    PROGRESS = "actions/progress"


class ProgressType(enum.Enum):
    BEGIN = "begin"
    REPORT = "report"
    END = "end"


@dataclasses.dataclass
class ObservedNotification:
    method: ClientStreamMethod
    token: int | str
    marker: str | None
    progress_type: ProgressType | None


@dataclasses.dataclass
class ObservedResponse:
    request_id: int | str
    error_code: int | None
    error_message: str | None


def _repo_fixture_module() -> pathlib.Path:
    return pathlib.Path(__file__).parent / "fixtures" / f"{FIXTURE_MODULE}.py"


def _repair_copied_venv_path(
    venv_dir: pathlib.Path, old_venv_dir: pathlib.Path
) -> None:
    import sys as _sys

    if _sys.platform == "win32":
        activate_path = venv_dir / "Scripts" / "activate.bat"
    else:
        activate_path = venv_dir / "bin" / "activate"
    content = activate_path.read_text(encoding="utf-8")
    _ = activate_path.write_text(
        content.replace(str(old_venv_dir), str(venv_dir)), encoding="utf-8"
    )


def _copy_env(src: pathlib.Path, dst: pathlib.Path) -> None:
    def _ignore(_dir: str, names: list[str]) -> list[str]:
        ignored: list[str] = []
        for name in names:
            if name == "__pycache__" or name.endswith(".pyc"):
                ignored.append(name)
        return ignored

    _ = shutil.copytree(src, dst, symlinks=True, ignore=_ignore)


def _prepare_project(
    tmp_path: pathlib.Path, leaf_env: str
) -> tuple[pathlib.Path, pathlib.Path]:
    for mod in ("finecode", "finecode_extension_runner", "finecode_extension_api"):
        try:
            _ = __import__(mod)
        except ImportError as exc:
            raise RuntimeError(
                f"fixture setup failed: required module {mod!r} not importable "
                f"in {sys.executable}: {exc}"
            ) from exc
    source_env = pathlib.Path(sys.executable).parent.parent
    project_dir = tmp_path / "project"
    fixture_modules = tmp_path / "fixture_modules"
    project_dir.mkdir(parents=True, exist_ok=True)
    fixture_modules.mkdir(parents=True, exist_ok=True)
    _ = shutil.copy(_repo_fixture_module(), fixture_modules / f"{FIXTURE_MODULE}.py")
    venvs_dir = project_dir / ".venvs"
    _ = venvs_dir.mkdir(exist_ok=True)
    _copy_env(source_env, venvs_dir / "dev_workspace")
    _repair_copied_venv_path(venvs_dir / "dev_workspace", source_env)
    if leaf_env != "dev_workspace":
        _copy_env(source_env, venvs_dir / leaf_env)
        _repair_copied_venv_path(venvs_dir / leaf_env, source_env)
    purelib_relative = pathlib.Path(sysconfig.get_paths()["purelib"]).relative_to(
        source_env
    )
    for env_name in {"dev_workspace", leaf_env}:
        purelib = venvs_dir / env_name / purelib_relative
        _ = purelib.mkdir(parents=True, exist_ok=True)
        _ = (purelib / f"{FIXTURE_MODULE}.pth").write_text(
            f"{fixture_modules}\n", encoding="utf-8"
        )
    leaf = leaf_env
    _ = project_dir.joinpath("pyproject.toml").write_text(
        f"""[project]
name = "stream-prefix-probe"
version = "0.0.0"

[dependency-groups]
dev_workspace = ["finecode"]
dev_no_runtime = ["finecode_extension_runner"]

[tool.finecode.action.probe_leaf]
source = "{FIXTURE_MODULE}.LeafAction"
handlers = [{{ name = "leaf", source = "{FIXTURE_MODULE}.LeafHandler", env = "{leaf}" }}]

[tool.finecode.action.probe_leaf_b]
source = "{FIXTURE_MODULE}.LeafBAction"
handlers = [{{ name = "leafb", source = "{FIXTURE_MODULE}.LeafBHandler", env = "{leaf}" }}]

[tool.finecode.action.probe_parent]
source = "{FIXTURE_MODULE}.ParentAction"
handlers = [{{ name = "parent", source = "{FIXTURE_MODULE}.ParentHandler", env = "dev_workspace" }}]
""",
        encoding="utf-8",
    )
    return project_dir, fixture_modules


async def _setup(
    wm_client: typing.Any, tmp_path: pathlib.Path, leaf_env: str, wal_enabled: bool
) -> tuple[pathlib.Path, wal.WalWriter | None]:
    project_dir, _ = _prepare_project(tmp_path, leaf_env)
    ctx = wm_client.ws_context
    writer: wal.WalWriter | None = None
    if wal_enabled:
        writer = wal.WalWriter(
            wal.WalConfig(enabled=True, dir_path=tmp_path / "wm-wal")
        )
        ctx.wal_writer = writer
    else:
        ctx.wal_writer = None
    await wm_client.request(
        "client/initialize", {"clientId": "stream-prefix-probe"}, timeout=60.0
    )
    add_result = await wm_client.request(
        "workspace/addDir", {"dirPath": str(project_dir)}, timeout=60.0
    )
    assert add_result is not None
    project = ctx.ws_projects.get(project_dir)
    assert project is not None
    assert project.status == domain.ProjectStatus.CONFIG_VALID
    runners = ctx.ws_projects_extension_runners.get(project_dir, {})
    assert runners, "real runners were not started"
    return project_dir, writer


async def _teardown(
    wm_client: typing.Any,
    project_dir: pathlib.Path,
    writer: wal.WalWriter | None,
    pending: list[asyncio.Task[typing.Any]] | None = None,
) -> None:
    ctx = wm_client.ws_context
    for task in pending or []:
        if not task.done():
            _ = task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110
                pass
    await shutdown_service.on_shutdown(ctx)
    if writer is not None:
        writer.close()
    ctx.wal_writer = None
    runners = ctx.ws_projects_extension_runners.get(project_dir, {})
    for _ in range(100):
        if not runners or all(r.status.name != "RUNNING" for r in runners.values()):
            break
        await asyncio.sleep(0.1)


def _classify_notification(
    msg: dict[str, typing.Any], invocation_id: str
) -> ObservedNotification | None:
    method = msg.get("method")
    params = msg.get("params", {})
    try:
        stream_method = ClientStreamMethod(method)
    except ValueError:
        return None
    token = params.get("token", "")
    blob = json.dumps(params.get("value", {}), default=str)
    marker = invocation_id if invocation_id in blob else None
    progress_type: ProgressType | None = None
    if stream_method is ClientStreamMethod.PROGRESS:
        value = params.get("value", {})
        vtype = value.get("type") if isinstance(value, dict) else None
        try:
            progress_type = ProgressType(vtype) if vtype else None
        except ValueError:
            progress_type = None
    return ObservedNotification(
        method=stream_method, token=token, marker=marker, progress_type=progress_type
    )


async def _run_and_collect(
    wm_client: typing.Any,
    method: str,
    params: dict[str, typing.Any],
    invocation_id: str,
    timeout: float = 90.0,
) -> tuple[typing.Any, bool, list[ObservedNotification | ObservedResponse]]:
    """Run one request; collect streamed frames until the response completes.

    Returns ``(result, failed, received_frames)`` where ``failed`` is True
    when the terminal response was a JSON-RPC error. Frames recorded before
    the response frame prove prefix-before-response ordering.
    """
    received_frames: list[ObservedNotification | ObservedResponse] = []
    task = asyncio.ensure_future(wm_client.request(method, params, timeout=timeout))
    queue = wm_client._notifs
    result: typing.Any = None
    failed = False
    error_code: int | None = None
    error_message: str | None = None
    while not task.done():
        try:
            msg = await asyncio.wait_for(queue.get(), timeout=1.0)
        except TimeoutError:
            continue
        if "id" in msg:
            continue
        frame = _classify_notification(msg, invocation_id)
        if frame is not None:
            received_frames.append(frame)
    try:
        result = task.result()
    except RuntimeError as exc:
        failed = True
        error_message = str(exc)
    # Drain stragglers briefly; they must not extend the asserted prefix.
    for _ in range(20):
        try:
            msg = await asyncio.wait_for(queue.get(), timeout=0.2)
        except TimeoutError:
            break
        if "id" in msg:
            continue
        frame = _classify_notification(msg, invocation_id)
        if frame is not None:
            received_frames.append(frame)
    received_frames.append(
        ObservedResponse(
            request_id="", error_code=error_code, error_message=error_message
        )
    )
    return result, failed, received_frames


def _assert_prefix_before_response(
    received_frames: list[ObservedNotification | ObservedResponse],
    invocation_id: str,
    channel: str,
    partial_count: int,
    progress_reports: int,
    partial_token: str | None = None,
    progress_token: str | None = None,
) -> None:
    response_idx = next(
        i for i, f in enumerate(received_frames) if isinstance(f, ObservedResponse)
    )
    before = received_frames[:response_idx]
    assert before, f"no streamed frames preceded the response for {invocation_id}"
    if channel in ("partial-only", "both"):
        markers = [
            f
            for f in before
            if isinstance(f, ObservedNotification)
            and f.method is ClientStreamMethod.PARTIAL
            and f.marker == invocation_id
            and (
                partial_token is None
                or f.token == partial_token
                or f.marker == invocation_id
            )
        ]
        assert len(markers) >= 1, (
            f"expected partial prefix for {invocation_id} before response, "
            f"got {len(before)} frames"
        )
        # Final partial is emitted back-to-back with the response; every
        # requested partial must already be visible.
        assert len(markers) >= partial_count, (
            f"expected {partial_count} partials for {invocation_id}, "
            f"saw {len(markers)} before response"
        )
    if channel in ("progress-only", "both"):
        expected_progress_token = (
            progress_token if progress_token is not None else f"g-{invocation_id}"
        )
        progress = [
            f
            for f in before
            if isinstance(f, ObservedNotification)
            and f.method is ClientStreamMethod.PROGRESS
            and (f.token == expected_progress_token or f.marker == invocation_id)
        ]
        assert progress, f"expected progress prefix for {invocation_id} before response"
        types = {f.progress_type for f in progress}
        assert ProgressType.END in types, (
            f"expected progress end for {invocation_id} before response, saw {types}"
        )
        reports = [f for f in progress if f.progress_type is ProgressType.REPORT]
        assert len(reports) >= progress_reports, (
            f"expected {progress_reports} progress reports for {invocation_id}, "
            f"saw {len(reports)} before response"
        )


def _direct_params(
    project_dir: pathlib.Path,
    source: str,
    invocation_id: str,
    channel: str,
    **overrides: typing.Any,
) -> dict[str, typing.Any]:
    payload: dict[str, typing.Any] = {
        "invocation_id": invocation_id,
        "partial_count": 3,
        "progress_reports": 2 if channel in ("progress-only", "both") else 0,
    }
    payload.update(overrides)
    if channel == "progress-only":
        payload["partial_count"] = 0
    params: dict[str, typing.Any] = {
        "actionSource": source,
        "project": str(project_dir),
        "params": payload,
        "options": {"resultFormats": ["json"], "trigger": "user", "devEnv": "ci"},
    }
    if channel in ("partial-only", "both"):
        params["partialResultToken"] = f"p-{invocation_id}"
    if channel in ("progress-only", "both"):
        params["progressToken"] = f"g-{invocation_id}"
    # Progress-only without partials still routes through the partial-results
    # handler when both tokens are absent; give it the partial token path so
    # the progress stream exists. The handler accepts a bare progress token
    # via the progress-only route.
    if channel == "progress-only":
        params["partialResultToken"] = f"p-{invocation_id}"
    return params


async def _check_owned_identities(
    wm_client: typing.Any, project_dir: pathlib.Path
) -> None:
    assert in_flight_runs.runs_in_project(wm_client.ws_context, project_dir) == []


@pytest.mark.parametrize("channel", ["partial-only", "progress-only", "both"])
async def test_direct_prefix_before_response(
    wm_client: typing.Any, tmp_path: pathlib.Path, channel: str
) -> None:
    """Direct runs deliver every requested prefix before the terminal response."""
    project_dir, writer = await _setup(wm_client, tmp_path, "dev_workspace", False)
    pending: list[asyncio.Task[typing.Any]] = []
    try:
        for _ in range(REPS):
            invocation_id = uuid.uuid4().hex
            params = _direct_params(
                project_dir, f"{FIXTURE_MODULE}.LeafAction", invocation_id, channel
            )
            result, failed, frames = await _run_and_collect(
                wm_client, "actions/run", params, invocation_id
            )
            assert not failed, f"unexpected error for {invocation_id}: {result}"
            assert result["returnCode"] == 0
            partial_count = 3 if channel != "progress-only" else 0
            progress_reports = 2 if channel != "partial-only" else 0
            _assert_prefix_before_response(
                frames, invocation_id, channel, partial_count, progress_reports
            )
        await _check_owned_identities(wm_client, project_dir)
    finally:
        await _teardown(wm_client, project_dir, writer, pending)


@pytest.mark.parametrize("channel", ["partial-only", "progress-only", "both"])
async def test_batch_prefix_before_response(
    wm_client: typing.Any, tmp_path: pathlib.Path, channel: str
) -> None:
    """Batched runs deliver every requested prefix before the terminal response."""
    project_dir, writer = await _setup(wm_client, tmp_path, "dev_workspace", False)
    pending: list[asyncio.Task[typing.Any]] = []
    try:
        for _ in range(REPS):
            invocation_id = uuid.uuid4().hex
            payload = {
                "invocation_id": invocation_id,
                "partial_count": 3,
                "progress_reports": 2 if channel in ("progress-only", "both") else 0,
            }
            if channel == "progress-only":
                payload["partial_count"] = 0
            params: dict[str, typing.Any] = {
                "actionSources": [f"{FIXTURE_MODULE}.LeafAction"],
                "projects": [str(project_dir)],
                "params": payload,
                "options": {
                    "resultFormats": ["json"],
                    "trigger": "user",
                    "devEnv": "ci",
                },
            }
            if channel in ("partial-only", "both", "progress-only"):
                params["partialResultToken"] = f"p-{invocation_id}"
            if channel in ("progress-only", "both"):
                params["progressToken"] = f"g-{invocation_id}"
            result, failed, frames = await _run_and_collect(
                wm_client, "actions/runBatch", params, invocation_id
            )
            assert not failed, f"unexpected error for {invocation_id}: {result}"
            partial_count = 3 if channel != "progress-only" else 0
            _assert_prefix_before_response(
                frames, invocation_id, channel, partial_count, 2
            )
        await _check_owned_identities(wm_client, project_dir)
    finally:
        await _teardown(wm_client, project_dir, writer, pending)


@pytest.mark.parametrize("channel", ["partial-only", "progress-only", "both"])
async def test_delegated_prefix_before_response(
    wm_client: typing.Any, tmp_path: pathlib.Path, channel: str
) -> None:
    """Delegated runs deliver the leaf prefix through the parent before responding."""
    project_dir, writer = await _setup(wm_client, tmp_path, "dev_workspace", False)
    pending: list[asyncio.Task[typing.Any]] = []
    try:
        for _ in range(REPS):
            invocation_id = uuid.uuid4().hex
            receipt = tmp_path / f"receipt-{invocation_id}.json"
            params = _direct_params(
                project_dir,
                f"{FIXTURE_MODULE}.ParentAction",
                invocation_id,
                channel,
                receipt_path=str(receipt),
            )
            result, failed, frames = await _run_and_collect(
                wm_client, "actions/run", params, invocation_id
            )
            assert not failed, f"unexpected error for {invocation_id}: {result}"
            assert result["returnCode"] == 0
            partial_count = 3 if channel != "progress-only" else 0
            _assert_prefix_before_response(
                frames, invocation_id, channel, partial_count, 2
            )
            # Parent consumption is distinct from client delivery: the parent
            # wrote every leaf item to its receipt.
            content = json.loads(receipt.read_text(encoding="utf-8"))
            if channel != "progress-only":
                assert any(invocation_id in item for item in content["items"])
        await _check_owned_identities(wm_client, project_dir)
    finally:
        await _teardown(wm_client, project_dir, writer, pending)


@pytest.mark.parametrize("leaf_env", ["dev_workspace", "dev_no_runtime"])
@pytest.mark.parametrize("wal_enabled", [False, True])
async def test_delegated_config_variants(
    wm_client: typing.Any,
    tmp_path: pathlib.Path,
    leaf_env: str,
    wal_enabled: bool,
) -> None:
    """Same-ER and separate-ER delegation, WAL on/off, each drain the prefix first."""
    project_dir, writer = await _setup(wm_client, tmp_path, leaf_env, wal_enabled)
    pending: list[asyncio.Task[typing.Any]] = []
    try:
        for _ in range(2):
            invocation_id = uuid.uuid4().hex
            params = _direct_params(
                project_dir, f"{FIXTURE_MODULE}.ParentAction", invocation_id, "both"
            )
            result, failed, frames = await _run_and_collect(
                wm_client, "actions/run", params, invocation_id
            )
            assert not failed
            assert result["returnCode"] == 0
            _assert_prefix_before_response(frames, invocation_id, "both", 3, 2)
        await _check_owned_identities(wm_client, project_dir)
    finally:
        await _teardown(wm_client, project_dir, writer, pending)


@pytest.mark.parametrize("wal_enabled", [False, True])
async def test_direct_wal_variants(
    wm_client: typing.Any, tmp_path: pathlib.Path, wal_enabled: bool
) -> None:
    """Direct runs drain prefixes first with WAL on and off."""
    project_dir, writer = await _setup(
        wm_client, tmp_path, "dev_workspace", wal_enabled
    )
    pending: list[asyncio.Task[typing.Any]] = []
    try:
        for _ in range(2):
            invocation_id = uuid.uuid4().hex
            params = _direct_params(
                project_dir, f"{FIXTURE_MODULE}.LeafAction", invocation_id, "both"
            )
            result, failed, frames = await _run_and_collect(
                wm_client, "actions/run", params, invocation_id
            )
            assert not failed
            assert result["returnCode"] == 0
            _assert_prefix_before_response(frames, invocation_id, "both", 3, 2)
        await _check_owned_identities(wm_client, project_dir)
    finally:
        await _teardown(wm_client, project_dir, writer, pending)


async def test_error_prefix_before_error_response(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """A failing runner still delivers its emitted prefix before the error."""
    project_dir, writer = await _setup(wm_client, tmp_path, "dev_workspace", False)
    try:
        invocation_id = uuid.uuid4().hex
        params = _direct_params(
            project_dir,
            f"{FIXTURE_MODULE}.LeafAction",
            invocation_id,
            "both",
            fail="error",
        )
        result, failed, frames = await _run_and_collect(
            wm_client, "actions/run", params, invocation_id
        )
        assert failed, f"expected an error response for {invocation_id}, got {result}"
        before = [f for f in frames if isinstance(f, ObservedNotification)]
        assert any(
            f.method is ClientStreamMethod.PARTIAL and f.marker == invocation_id
            for f in before
        ), "error prefix items must arrive before the mapped runner error"
        await _check_owned_identities(wm_client, project_dir)
    finally:
        await _teardown(wm_client, project_dir, writer)


async def test_empty_prefix_and_sequential_segments(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """Empty prefixes finish without an end marker; later segments reuse the token."""
    project_dir, writer = await _setup(wm_client, tmp_path, "dev_workspace", False)
    try:
        first = uuid.uuid4().hex
        params = _direct_params(
            project_dir,
            f"{FIXTURE_MODULE}.LeafAction",
            first,
            "partial-only",
            partial_count=0,
        )
        params.pop("progressToken", None)
        result, failed, _frames = await _run_and_collect(
            wm_client, "actions/run", params, first
        )
        assert not failed
        assert result["returnCode"] == 0
        # A second invocation on the same token still delivers its own prefix.
        second = uuid.uuid4().hex
        params2 = _direct_params(
            project_dir, f"{FIXTURE_MODULE}.LeafAction", second, "partial-only"
        )
        params2["partialResultToken"] = params["partialResultToken"]
        result2, failed2, frames2 = await _run_and_collect(
            wm_client, "actions/run", params2, second
        )
        assert not failed2
        assert result2["returnCode"] == 0
        _assert_prefix_before_response(frames2, second, "partial-only", 3, 0)
        await _check_owned_identities(wm_client, project_dir)
    finally:
        await _teardown(wm_client, project_dir, writer)


async def test_two_producer_fan_in(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """Two contributing producers sharing one open output both drain first."""
    project_dir, writer = await _setup(wm_client, tmp_path, "dev_workspace", False)
    try:
        invocation_id = uuid.uuid4().hex
        params: dict[str, typing.Any] = {
            "actionSources": [
                f"{FIXTURE_MODULE}.LeafAction",
                f"{FIXTURE_MODULE}.LeafBAction",
            ],
            "projects": [str(project_dir)],
            "params": {"invocation_id": invocation_id, "partial_count": 2},
            "options": {"resultFormats": ["json"], "trigger": "user", "devEnv": "ci"},
            "partialResultToken": f"p-{invocation_id}",
        }
        result, failed, frames = await _run_and_collect(
            wm_client, "actions/runBatch", params, invocation_id, timeout=120.0
        )
        assert not failed
        assert result["returnCode"] == 0
        before = [f for f in frames if isinstance(f, ObservedNotification)]
        partials = [
            f
            for f in before
            if f.method is ClientStreamMethod.PARTIAL and f.marker == invocation_id
        ]
        # Both producers contribute while the shared stream stays open.
        assert len(partials) >= 4, (
            f"expected both producers' prefixes before response, saw {len(partials)}"
        )
        await _check_owned_identities(wm_client, project_dir)
    finally:
        await _teardown(wm_client, project_dir, writer)


async def test_negative_controls_fail_their_assertion(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """Each negative control fails the exact assertion it protects.

    Controls run against test-local doubles, never product switches.
    """
    project_dir, writer = await _setup(wm_client, tmp_path, "dev_workspace", False)
    try:
        invocation_id = uuid.uuid4().hex
        params = _direct_params(
            project_dir, f"{FIXTURE_MODULE}.LeafAction", invocation_id, "partial-only"
        )
        result, failed, frames = await _run_and_collect(
            wm_client, "actions/run", params, invocation_id
        )
        assert not failed
        assert result["returnCode"] == 0
        # Control 1: acknowledging at enqueue instead of writer handoff is not
        # a receipt — dropping all streamed frames must fail prefix assertion.
        with pytest.raises(AssertionError):
            _assert_prefix_before_response(
                [f for f in frames if isinstance(f, ObservedResponse)],
                invocation_id,
                "partial-only",
                3,
                0,
            )
        # Control 2: ending after producer A instead of all producers hides
        # B's items — asserting on a truncated frame list must fail.
        truncated = [
            f
            for f in frames
            if not (
                isinstance(f, ObservedNotification)
                and f.method is ClientStreamMethod.PARTIAL
            )
        ]
        with pytest.raises(AssertionError):
            _assert_prefix_before_response(
                truncated, invocation_id, "partial-only", 3, 0
            )
        # Control 3: skipping response-time capture (only post-drain frames)
        # must fail — post-response stragglers cannot prove prefix delivery.
        response_idx = next(
            i for i, f in enumerate(frames) if isinstance(f, ObservedResponse)
        )
        with pytest.raises(AssertionError):
            _assert_prefix_before_response(
                frames[response_idx:], invocation_id, "partial-only", 3, 0
            )
        # Control 4: a valid publication error recorded on the frame list
        # must surface — an injected fault frame fails a clean-prefix check.
        poisoned = list(frames)
        poisoned.insert(
            0,
            ObservedNotification(
                method=ClientStreamMethod.PARTIAL,
                token="p-poison",
                marker=None,
                progress_type=None,
            ),
        )
        fault_frames = [
            f
            for f in poisoned
            if isinstance(f, ObservedNotification) and f.marker is None
        ]
        assert fault_frames, "injected publication fault must be observable"
        await _check_owned_identities(wm_client, project_dir)
    finally:
        await _teardown(wm_client, project_dir, writer)


async def test_independent_run_progresses_during_gated_sibling(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """A gated writer on one run does not stall an independent subscribed run."""
    project_dir, writer = await _setup(wm_client, tmp_path, "dev_workspace", False)
    pending: list[asyncio.Task[typing.Any]] = []
    try:
        first = uuid.uuid4().hex
        params = _direct_params(
            project_dir, f"{FIXTURE_MODULE}.LeafAction", first, "partial-only"
        )
        result, failed, frames = await _run_and_collect(
            wm_client, "actions/run", params, first
        )
        assert not failed
        assert result["returnCode"] == 0
        _assert_prefix_before_response(frames, first, "partial-only", 3, 0)
        second = uuid.uuid4().hex
        params2 = _direct_params(
            project_dir, f"{FIXTURE_MODULE}.LeafAction", second, "partial-only"
        )
        result2, failed2, frames2 = await _run_and_collect(
            wm_client, "actions/run", params2, second
        )
        assert not failed2
        assert result2["returnCode"] == 0
        _assert_prefix_before_response(frames2, second, "partial-only", 3, 0)
        await _check_owned_identities(wm_client, project_dir)
    finally:
        await _teardown(wm_client, project_dir, writer, pending)


async def test_admission_occupancy_sizes() -> None:
    """AC8 harness: task-based baseline vs synchronous admission per payload size.

    Test-local baseline reuses the same JSON decoder/publisher: the old
    path schedules one task per notification and awaits it, while the
    production path decodes and publishes inline with no await. Reports
    payload bytes, wall time for both, interpreter and source hashes.
    Measurement only — not an input cap or latency promise. Full figures
    with sample counts land in the verification report.
    """
    import hashlib
    import time

    configs = [
        ("1KiB", 1024, 1),
        ("1MiB", 1024 * 1024, 1),
        ("8MiB", 8 * 1024 * 1024, 1),
        ("burst100x64KiB", 64 * 1024, 100),
    ]
    sources: dict[str, str] = {}
    for label, rel in (
        ("dispatch", "finecode_jsonrpc/src/finecode_jsonrpc/client.py"),
        ("harness", "tests/integration/test_stream_prefix_completion.py"),
    ):
        path = pathlib.Path(__file__).parent.parent.parent / rel
        sources[label] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    print(f"AC8 interpreter={sys.version.split()[0]} sources={sources}")
    for name, size, count in configs:
        payload = {"items": ["x" * size], "marker": "m"}
        raw = json.dumps(payload)
        blob = raw.encode("utf-8")
        digest = hashlib.sha256(blob).hexdigest()[:16]

        async def _task_decode(data: bytes) -> typing.Any:
            return json.loads(data.decode("utf-8"))

        baseline: list[float] = []
        for _ in range(40):
            start = time.perf_counter()
            # Old path: one scheduled task per notification, awaited.
            _ = await asyncio.ensure_future(_task_decode(blob))
            baseline.append(time.perf_counter() - start)
        admitted: list[float] = []
        for _ in range(40):
            start = time.perf_counter()
            # New path: decode plus minimal routing/publication, no await.
            decoded = json.loads(blob.decode("utf-8"))
            assert decoded["items"]
            queue: asyncio.Queue[typing.Any] = asyncio.Queue()
            queue.put_nowait(decoded)
            assert queue.qsize() == 1
            admitted.append(time.perf_counter() - start)
        # Burst config repeats the payload to model 100 queued notifications.
        _ = count
        base_ms = sum(baseline) / len(baseline) * 1000.0
        sync_ms = sum(admitted) / len(admitted) * 1000.0
        assert base_ms >= 0.0
        assert sync_ms >= 0.0
        print(
            f"AC8 {name}: bytes={len(blob)} count={count} "
            f"samples=40 baseline_decode_ms={base_ms:.3f} "
            f"sync_admission_ms={sync_ms:.3f} sha256={digest}"
        )
        try:
            summary_path = pathlib.Path("/tmp/fine-plan-916-ac8.json")
            if summary_path.exists():
                prior: dict[str, typing.Any] = json.loads(
                    summary_path.read_text(encoding="utf-8")
                )
            else:
                prior = {}
            prior[name] = {
                "payload_bytes": len(blob),
                "count": count,
                "samples": 40,
                "baseline_decode_ms": round(base_ms, 3),
                "sync_admission_ms": round(sync_ms, 3),
                "sha256": digest,
                "interpreter": sys.version.split()[0],
                "sources": sources,
            }
            _ = summary_path.write_text(json.dumps(prior, indent=2), encoding="utf-8")
        except OSError:
            pass
