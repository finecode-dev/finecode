import dataclasses
import pathlib
import re
from datetime import datetime

from finecode_extension_api import code_action
from finecode_extension_api.actions.observability.get_service_logs_action import (
    GetServiceLogsAction,
    GetServiceLogsRunContext,
    GetServiceLogsRunPayload,
    GetServiceLogsRunResult,
)
from finecode_extension_api.interfaces import (
    iextensionrunnerinfoprovider,
    ilogger,
)

from finecode_builtin_handlers.observability_log_utils import resolve_log_dir

_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2})")


def _log_file_sort_key(f: pathlib.Path) -> int:
    """Sort log files by their numeric rotation ID (e.g. 'runner_1.log' → 1)."""
    stem = f.stem
    parts = stem.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return int(parts[1])
    return 0


def _read_log_lines(log_dir: pathlib.Path) -> list[str]:
    files = sorted(log_dir.glob("*.log"), key=_log_file_sort_key)
    lines: list[str] = []
    for f in files:
        try:
            lines.extend(f.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            pass
    return lines


def _parse_line_ts(line: str) -> datetime | None:
    m = _TS_RE.match(line)
    if not m:
        return None
    try:
        return datetime.fromisoformat(m.group(1).replace(" ", "T"))
    except ValueError:
        return None


def _filter_since(lines: list[str], since_ts_iso: str) -> list[str]:
    """Best-effort: keep lines at or after since_ts_iso. Lines without a parseable
    timestamp are always kept (they may be continuation lines of a log entry)."""
    try:
        since_dt = datetime.fromisoformat(since_ts_iso.replace("Z", "+00:00"))
        # Strip timezone for naive comparison — log timestamps are local time
        since_dt = since_dt.replace(tzinfo=None)
    except ValueError:
        return lines

    result: list[str] = []
    for line in lines:
        ts = _parse_line_ts(line)
        if ts is None or ts >= since_dt:
            result.append(line)
    return result


@dataclasses.dataclass
class GetServiceLogsHandlerConfig(code_action.ActionHandlerConfig): ...


class GetServiceLogsHandler(
    code_action.ActionHandler[
        GetServiceLogsAction,
        GetServiceLogsHandlerConfig,
    ]
):
    def __init__(
        self,
        runner_info_provider: iextensionrunnerinfoprovider.IExtensionRunnerInfoProvider,
        logger: ilogger.ILogger,
    ) -> None:
        self.runner_info_provider = runner_info_provider
        self.logger = logger

    async def run(
        self,
        payload: GetServiceLogsRunPayload,
        run_context: GetServiceLogsRunContext,
    ) -> GetServiceLogsRunResult:
        log_dir = resolve_log_dir(payload.service_id, self.runner_info_provider)
        if not log_dir.is_dir():
            return GetServiceLogsRunResult(
                service_id=payload.service_id,
                errors=[f"No logs directory found for service '{payload.service_id}'."],
            )

        lines = _read_log_lines(log_dir)

        if payload.since_ts_iso is not None:
            lines = _filter_since(lines, payload.since_ts_iso)

        total = len(lines)

        # Pagination: select a window of lines counting back from the most recent end.
        # offset_lines skips the last N lines before applying the tail window.
        end = max(0, total - payload.offset_lines)
        if payload.tail_lines is not None:
            start = end - payload.tail_lines
            truncated = start > 0
            start = max(0, start)
        else:
            start = 0
            truncated = False

        return GetServiceLogsRunResult(
            service_id=payload.service_id,
            content="\n".join(lines[start:end]),
            truncated=truncated,
        )
