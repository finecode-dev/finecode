from __future__ import annotations

import dataclasses
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path

from finecode_extension_api import code_action
from finecode_extension_api.actions.testing.run_tests_action import (
    RunTestsAction,
    RunTestsRunContext,
    RunTestsRunPayload,
    RunTestsRunResult,
    TestCaseResult,
    TestOutcome,
)
from finecode_extension_api.actions.testing.test_id import TestId
from finecode_extension_api.interfaces import (
    icommandrunner,
    ilogger,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import (
    ResourceUri,
    path_to_resource_uri,
    resource_uri_to_path,
)


@dataclasses.dataclass
class PytestRunTestsHandlerConfig(code_action.ActionHandlerConfig):
    # Extra pytest CLI arguments forwarded verbatim (e.g. ["-x", "--timeout=30"])
    addopts: list[str] = dataclasses.field(default_factory=list)


class PytestRunTestsHandler(
    code_action.ActionHandler[RunTestsAction, PytestRunTestsHandlerConfig]
):
    def __init__(
        self,
        config: PytestRunTestsHandlerConfig,
        logger: ilogger.ILogger,
        command_runner: icommandrunner.ICommandRunner,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
    ) -> None:
        self.config = config
        self.logger = logger
        self.command_runner = command_runner
        self.project_info_provider = project_info_provider
        self.pytest_bin = str(Path(sys.executable).parent / "pytest")

    async def run(
        self,
        payload: RunTestsRunPayload,
        run_context: RunTestsRunContext,
    ) -> RunTestsRunResult:
        project_dir = self.project_info_provider.get_current_project_dir_path()

        fd, report_path_str = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        report_path = Path(report_path_str)

        try:
            cmd_parts = [
                self.pytest_bin,
                "--json-report",
                f"--json-report-file={report_path}",
            ]

            # test_ids take priority over file_paths
            if payload.test_ids:
                cmd_parts.extend(
                    _test_id_to_node_id(t, project_dir) for t in payload.test_ids
                )
            elif payload.file_paths:
                cmd_parts.extend(
                    str(resource_uri_to_path(uri)) for uri in payload.file_paths
                )

            if payload.markers:
                cmd_parts.extend(["-m", " or ".join(payload.markers)])

            cmd_parts.extend(self.config.addopts)

            cmd = shlex.join(cmd_parts)
            self.logger.debug(f"Running pytest: {cmd}")

            async with run_context.progress("Running tests") as progress:
                process = await self.command_runner.run(cmd, cwd=project_dir)
                await progress.report("Tests running")
                await process.wait_for_end()

            if not report_path.exists() or report_path.stat().st_size == 0:
                error_output = process.get_error_output() or process.get_output()
                raise code_action.ActionFailedException(
                    f"pytest did not produce a JSON report. Output:\n{error_output}"
                )

            report_data = json.loads(report_path.read_text(encoding="utf-8"))
        finally:
            report_path.unlink(missing_ok=True)

        test_results: list[TestCaseResult] = []

        for test in report_data.get("tests", []):
            test_results.append(_map_test(test, project_dir))

        # Collection errors (e.g. import errors) appear as failed collectors
        for collector in report_data.get("collectors", []):
            if collector.get("outcome") == "failed":
                test_results.append(_map_collector_error(collector, project_dir))

        return RunTestsRunResult(test_results=test_results)


def _map_test(test: dict, project_dir: Path) -> TestCaseResult:
    nodeid: str = test["nodeid"]
    outcome = _map_outcome(test.get("outcome", "error"))

    test_id = _parse_nodeid(nodeid, project_dir)
    file_uri = test_id.file_path

    # Sum wall-clock durations across all phases
    duration = sum(
        test.get(phase, {}).get("duration", 0.0)
        for phase in ("setup", "call", "teardown")
    )

    # Extract failure/error message from the first phase that has one
    message: str | None = None
    if outcome in (TestOutcome.FAILED, TestOutcome.ERROR):
        for phase in ("call", "setup", "teardown"):
            longrepr = test.get(phase, {}).get("longrepr")
            if longrepr:
                message = str(longrepr)
                break

    # pytest-json-report exposes item.location[1], which is 0-based — matches
    # the LSP/TestCaseResult convention directly, no adjustment needed.
    line: int | None = test.get("lineno")

    return TestCaseResult(
        test_id=test_id,
        outcome=outcome,
        duration_seconds=duration or None,
        message=message,
        file_path=file_uri,
        line=line,
    )


def _map_collector_error(collector: dict, project_dir: Path) -> TestCaseResult:
    nodeid = collector.get("nodeid", "unknown")
    longrepr = collector.get("longrepr")

    test_id = _parse_nodeid(nodeid, project_dir)
    file_uri: ResourceUri | None = None
    file_path = resource_uri_to_path(test_id.file_path)
    if file_path.suffix == ".py":
        file_uri = test_id.file_path

    return TestCaseResult(
        test_id=test_id,
        outcome=TestOutcome.ERROR,
        message=str(longrepr) if longrepr else "Collection error",
        file_path=file_uri,
    )


def _map_outcome(outcome_str: str) -> TestOutcome:
    return {
        "passed": TestOutcome.PASSED,
        "failed": TestOutcome.FAILED,
        "skipped": TestOutcome.SKIPPED,
        "error": TestOutcome.ERROR,
    }.get(outcome_str.lower(), TestOutcome.ERROR)


def _parse_nodeid(nodeid: str, project_dir: Path) -> TestId:
    """Convert a pytest node ID to a unified ``TestId``.

    For leaf test results (from ``_map_test``) the last segment is always
    a test function/method, never a class, so it is placed in ``test_name``.

    The relative file path from the node ID is resolved against *project_dir*
    to produce an absolute ``file://`` ResourceUri.
    """
    parts = nodeid.split("::")
    file_part = parts[0]
    file_uri = path_to_resource_uri(project_dir / file_part)
    if len(parts) == 1:
        return TestId(file_path=file_uri)
    if len(parts) == 2:
        test_name, variant = _split_variant(parts[1])
        return TestId(file_path=file_uri, test_name=test_name, variant=variant)
    # len >= 3: file::Class::method[variant]
    test_name, variant = _split_variant(parts[-1])
    return TestId(file_path=file_uri, class_name=parts[1], test_name=test_name, variant=variant)


def _split_variant(name: str) -> tuple[str, str | None]:
    """Split ``"test_bar[p1-p2]"`` into ``("test_bar", "[p1-p2]")``."""
    idx = name.find("[")
    if idx == -1:
        return name, None
    return name[:idx], name[idx:]


def _test_id_to_node_id(test_id: TestId, project_dir: Path) -> str:
    """Convert a unified ``TestId`` back to a pytest node ID string.

    The ``file://`` URI in ``test_id.file_path`` is converted to a path
    relative to *project_dir* so that pytest receives the format it expects.
    """
    file_path = resource_uri_to_path(test_id.file_path)
    try:
        rel_path = file_path.relative_to(project_dir)
    except ValueError:
        rel_path = file_path
    parts = [str(rel_path)]
    if test_id.class_name:
        parts.append(test_id.class_name)
    if test_id.test_name:
        name = test_id.test_name
        if test_id.variant:
            name += test_id.variant
        parts.append(name)
    return "::".join(parts)
