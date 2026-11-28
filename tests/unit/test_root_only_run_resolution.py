"""AC1: a run whose every action is root-hosted and workspace-scoped resolves
the root only.

The CLI's WM request sequence for such a run — ``list_actions(names=[...])``,
``getPayloadSchemas`` for the root, then the ``runBatch`` resolution stage —
must never resolve a sibling project, because the run has no project of its
own to run in that would reach one.
"""

from __future__ import annotations

import pathlib

import pytest
from resolution_fake import SwappingResolver, make_preset_action

from finecode.wm_server import context, domain
from finecode.wm_server._api_handlers._actions import _handle_get_payload_schemas
from finecode.wm_server._api_handlers._helpers import _resolve_actions_by_project
from finecode.wm_server._api_handlers._workspace import _handle_list_actions


def _make_collected(path: pathlib.Path) -> domain.CollectedProject:
    return domain.CollectedProject(
        name=path.name,
        dir_path=path,
        def_path=path / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
        env_configs={},
        actions=[],
        services=[],
        action_handler_configs={},
    )


_ROOT_ONLY_ACTION = make_preset_action(
    name="root_only_action",
    source="fine_test.RootOnlyAction",
    scope=domain.ActionScope.WORKSPACE,
)


async def test_cli_sequence_for_root_hosted_run_resolves_root_only(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI's request sequence for a root-hosted workspace-scoped run
    resolves the root alone — the sibling path appears in no resolution call."""
    root = tmp_path / "root"
    sibling = tmp_path / "sibling"
    ws_context = context.WorkspaceContext(ws_dirs_paths=[root])
    ws_context.ws_projects[root] = _make_collected(root)
    ws_context.ws_projects[sibling] = _make_collected(sibling)
    resolver = SwappingResolver(
        ws_context, monkeypatch, preset_actions=[_ROOT_ONLY_ACTION]
    )

    # 1. The CLI's listing: names filter, nothing else.
    listing = await _handle_list_actions({"names": ["root_only_action"]}, ws_context)
    assert [a["name"] for a in listing["actions"]] == ["root_only_action"]
    assert listing["unresolvedProjects"] == []

    # 2. The schema fetch for the workspace root.
    await _handle_get_payload_schemas(
        {
            "project": str(root),
            "actionSources": ["fine_test.RootOnlyAction"],
        },
        ws_context,
    )

    # 3. The runBatch resolution stage.
    actions_by_project, _ = await _resolve_actions_by_project(
        None, ["fine_test.RootOnlyAction"], ws_context
    )
    assert actions_by_project == {root: ["root_only_action"]}

    # The sibling was never resolved; dispatch is the handler's own business.
    assert resolver.calls == [[root]]
    assert sibling not in resolver.calls[0]
