from __future__ import annotations

import dataclasses
import shlex
import sys
from pathlib import Path

from finecode_extension_api import code_action
from finecode_extension_api.actions.testing.list_tests_action import (
    ListTestsAction,
    ListTestsRunContext,
    ListTestsRunPayload,
    ListTestsRunResult,
    TestItem,
)
from finecode_extension_api.actions.testing.test_id import TestId
from finecode_extension_api.interfaces import (
    icommandrunner,
    ilogger,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import (
    path_to_resource_uri,
    resource_uri_to_path,
)


@dataclasses.dataclass
class PytestListTestsHandlerConfig(code_action.ActionHandlerConfig):
    # Extra pytest CLI arguments forwarded verbatim to --collect-only
    addopts: list[str] = dataclasses.field(default_factory=list)


class PytestListTestsHandler(
    code_action.ActionHandler[ListTestsAction, PytestListTestsHandlerConfig]
):
    def __init__(
        self,
        config: PytestListTestsHandlerConfig,
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
        payload: ListTestsRunPayload,
        run_context: ListTestsRunContext,
    ) -> ListTestsRunResult:
        project_dir = self.project_info_provider.get_current_project_dir_path()

        cmd_parts = [
            self.pytest_bin,
            "--collect-only",
            "-q",
        ]

        if payload.file_paths:
            cmd_parts.extend(
                str(resource_uri_to_path(uri)) for uri in payload.file_paths
            )

        cmd_parts.extend(self.config.addopts)

        cmd = shlex.join(cmd_parts)
        self.logger.debug(f"Running pytest collect: {cmd}")

        async with run_context.progress("Discovering tests") as progress:
            process = await self.command_runner.run(cmd, cwd=project_dir)
            await progress.report("Collecting tests")
            await process.wait_for_end()

        output = process.get_output() or ""
        node_ids = _parse_collect_output(output)
        tests = _build_tree(node_ids, project_dir)

        return ListTestsRunResult(tests=tests)


def _parse_collect_output(output: str) -> list[str]:
    """Extract pytest node IDs from --collect-only -q output.

    The output looks like:
        tests/test_foo.py::TestClass::test_method
        tests/test_foo.py::test_function

        2 tests collected in 0.05s

    Error and summary lines are filtered out.
    """
    node_ids: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        # Skip summary line ("N tests collected in Xs" or "no tests ran")
        if "collected" in line or line.startswith("no tests ran"):
            continue
        # Skip collection error lines
        if line.startswith("ERROR"):
            continue
        # Valid lines are node IDs: contain "::" or are bare .py paths
        if "::" in line or line.endswith(".py"):
            node_ids.append(line)
    return node_ids


def _build_tree(node_ids: list[str], project_dir: Path) -> list[TestItem]:
    """Build a file → class (optional) → function tree from pytest node IDs."""
    # Preserve insertion order for files
    files: dict[str, TestItem] = {}

    for node_id in node_ids:
        parts = node_id.split("::")
        file_part = parts[0]
        file_uri = path_to_resource_uri(project_dir / file_part)

        if file_part not in files:
            files[file_part] = TestItem(
                test_id=TestId(file_path=file_uri),
                display_name=file_part,
                file_path=file_uri,
            )
        file_node = files[file_part]

        if len(parts) == 2:
            # file::test_function (leaf, no class)
            test_name, variant = _split_variant(parts[1])
            file_node.children.append(
                TestItem(
                    test_id=TestId(file_path=file_uri, test_name=test_name, variant=variant),
                    display_name=parts[1],
                    file_path=file_uri,
                )
            )
        elif len(parts) >= 3:
            # file::Class::method (pytest doesn't nest deeper than this)
            class_id = TestId(file_path=file_uri, class_name=parts[1])
            class_node = next(
                (c for c in file_node.children if c.test_id == class_id),
                None,
            )
            if class_node is None:
                class_node = TestItem(
                    test_id=class_id,
                    display_name=parts[1],
                    file_path=file_uri,
                )
                file_node.children.append(class_node)

            test_name, variant = _split_variant(parts[-1])
            class_node.children.append(
                TestItem(
                    test_id=TestId(
                        file_path=file_uri,
                        class_name=parts[1],
                        test_name=test_name,
                        variant=variant,
                    ),
                    display_name="::".join(parts[2:]),
                    file_path=file_uri,
                )
            )

    return list(files.values())


def _split_variant(name: str) -> tuple[str, str | None]:
    """Split ``"test_bar[p1-p2]"`` into ``("test_bar", "[p1-p2]")``."""
    idx = name.find("[")
    if idx == -1:
        return name, None
    return name[:idx], name[idx:]
