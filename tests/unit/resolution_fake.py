"""Shared fake for resolution-gate tests (steps 4-7 of plan 57).

``SwappingResolver`` stands in for ``runner_start_service.start_runners_with_auto_prepare``
and reproduces the behaviour the real first pass has that makes a stale project
reference a bug: it *replaces* each project in ``ws_context.ws_projects`` with a
fresh ``ResolvedProject`` object (exactly like the real second pass,
``runner_manager.py:1113-1114``) and writes ``ws_projects_raw_configs``.  Every
swapped project carries extra "preset" actions — actions the raw config did not
declare, which therefore exist only on the new object — so any test that reads a
project reference taken before the gate fails.
"""

from __future__ import annotations

import asyncio
import copy
import pathlib
import typing

import pytest

from finecode.wm_server import context, domain
from finecode.wm_server.runner import runner_manager
from finecode.wm_server.services import runner_start_service


def make_preset_action(
    *,
    name: str,
    source: str,
    scope: domain.ActionScope,
    handler_env: str = "dev_workspace",
    no_metadata: bool = False,
) -> domain.Action:
    """A preset-contributed action: present only on the swapped object, with
    class-level metadata already resolved unless ``no_metadata`` is asked."""
    handler = domain.ActionHandler(
        name="h",
        source="test.handlers.TestHandler",
        config={},
        env=handler_env,
        dependencies=[],
    )
    action = domain.Action(name=name, source=source, handlers=[handler], config={})
    if not no_metadata:
        action.canonical_source = source
        action.scope = scope
    return action


class SwappingResolver:
    """Stand-in for ``runner_start_service.start_runners_with_auto_prepare``.

    Records each call's project paths; resolves every path except
    ``fail_paths`` by swapping in a fresh ``ResolvedProject``; raises
    ``ProjectsFailedToResolve`` (or the bare exception for a one-path call)
    when any requested path is a failure, mirroring
    ``start_runners_with_presets`` (B rule 4).  Can block on ``block`` so tests
    can hold a resolution open and observe concurrent calls.
    """

    def __init__(
        self,
        ws_context: context.WorkspaceContext,
        monkeypatch: pytest.MonkeyPatch,
        *,
        preset_actions: list[domain.Action] | None = None,
        fail_paths: set[pathlib.Path] | None = None,
        fail_message: str = "runner start failed",
        raise_bare: bool = False,
        block: asyncio.Event | None = None,
    ) -> None:
        self.ws_context = ws_context
        self.preset_actions = list(preset_actions or [])
        self.fail_paths = set(fail_paths or [])
        self.fail_message = fail_message
        self.raise_bare = raise_bare
        self.block = block
        self.calls: list[list[pathlib.Path]] = []
        monkeypatch.setattr(
            runner_start_service, "start_runners_with_auto_prepare", self._fake
        )

    async def _fake(
        self,
        *,
        projects: list[domain.Project],
        ws_context: context.WorkspaceContext,
        initialize_all_handlers: bool = False,
        **kwargs: object,
    ) -> None:
        paths = [project.dir_path for project in projects]
        self.calls.append(paths)
        failed = [p for p in paths if p in self.fail_paths]
        for p in paths:
            if p not in failed:
                self._swap_project(p)
        # Block *after* the swap: the real code stores the ResolvedProject
        # before the dev_workspace runner has received its preset config, and
        # the wait-first rule says a second caller must still wait in that
        # window.
        if self.block is not None:
            await self.block.wait()
        if failed:
            per_project = {
                p: runner_manager.RunnerFailedToStart(self.fail_message) for p in failed
            }
            if len(paths) == 1 or self.raise_bare:
                # For a one-path batch, or a bare failure a test wants to be
                # unattributable, raise the plain exception (B rule 4).
                raise next(iter(per_project.values()))
            raise runner_manager.ProjectsFailedToResolve(
                per_project=per_project,
                message="Failed to start runner(s) for: "
                + ", ".join(str(p) for p in failed)
                + ".",
            )

    def _swap_project(self, path: pathlib.Path) -> None:
        old = self.ws_context.ws_projects[path]
        actions = list(old.actions) + [copy.copy(a) for a in self.preset_actions]
        collected = domain.CollectedProject(
            name=old.name,
            dir_path=old.dir_path,
            def_path=old.def_path,
            status=domain.ProjectStatus.CONFIG_VALID,
            env_configs=dict(old.env_configs),
            actions=actions,
            services=old.services,
            action_handler_configs=dict(old.action_handler_configs),
        )
        # Replacement, not mutation — a reference held before the swap must go stale.
        self.ws_context.ws_projects[path] = domain.ResolvedProject.from_collected(
            collected
        )
        self.ws_context.ws_projects_raw_configs[path] = {"tool": {"finecode": {}}}
