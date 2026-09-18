"""Tests for how the shared ruff server gets its settings.

Ruff reads client settings only during the ``initialize`` handshake -- its
``workspace/didChangeConfiguration`` handler does nothing -- so anything registered
after the server started is lost. One service instance is shared by every ruff handler,
so what is being pinned down here is that no handler can start the server before all of
them have contributed.
"""

from __future__ import annotations

import asyncio
import typing

from finecode_extension_api import code_action

from fine_python_ruff.ruff_lsp_service import (
    _RUFF_CLIENT_CAPABILITIES,
    RuffLspService,
)

_META = code_action.RunActionMeta(
    trigger=code_action.RunActionTrigger.USER, dev_env=code_action.DevEnv.CLI
)


class _StubLspService:
    """Records the settings the server would have been started with."""

    def __init__(self) -> None:
        self.settings: dict[str, typing.Any] = {}
        self.settings_at_start: dict[str, typing.Any] | None = None
        self.starts = 0

    def update_settings(self, settings: dict[str, typing.Any]) -> None:
        self.settings.update(settings)

    async def ensure_started(self, root_uri: str) -> None:
        if self.settings_at_start is None:
            self.settings_at_start = dict(self.settings)
        self.starts += 1


class _CollectingLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def exception(self, exception: Exception) -> None: ...
    def trace(self, message: str) -> None: ...
    def info(self, message: str) -> None: ...
    def debug(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...
    def disable(self, package: str) -> None: ...
    def enable(self, package: str) -> None: ...

    def warning(self, message: str) -> None:
        self.warnings.append(message)


def _service() -> tuple[RuffLspService, _StubLspService, _CollectingLogger]:
    logger = _CollectingLogger()
    service = RuffLspService(
        lsp_client=typing.cast(typing.Any, object()),
        file_editor=typing.cast(typing.Any, object()),
        logger=typing.cast(typing.Any, logger),
    )
    stub = _StubLspService()
    service._lsp_service = typing.cast(typing.Any, stub)
    return service, stub, logger


async def test_every_handlers_settings_are_in_place_before_the_server_starts() -> None:
    # the formatter starting the server must not cost the linter its rule selection:
    # format-on-save alone decides which handler gets there first
    service, stub, _ = _service()

    async def lint_settings(meta: code_action.RunActionMeta) -> dict[str, typing.Any]:
        return {"lint": {"extendSelect": ["B"]}, "showSyntaxErrors": True}

    async def format_settings(meta: code_action.RunActionMeta) -> dict[str, typing.Any]:
        return {"format": {"preview": True}}

    service.add_settings_provider(lint_settings)
    service.add_settings_provider(format_settings)

    await service.ensure_started("file:///project", _META)

    assert stub.settings_at_start == {
        "lint": {"extendSelect": ["B"]},
        "showSyntaxErrors": True,
        "format": {"preview": True},
    }


async def test_contributions_to_one_table_do_not_replace_each_other() -> None:
    # the linter fills configuration.target-version and the formatter configuration
    # .format; a flat merge would let whichever ran last drop the other's entry
    service, stub, _ = _service()

    async def from_linter(meta: code_action.RunActionMeta) -> dict[str, typing.Any]:
        return {"configuration": {"target-version": "py311"}}

    async def from_formatter(meta: code_action.RunActionMeta) -> dict[str, typing.Any]:
        return {"configuration": {"format": {"quote-style": "single"}}}

    service.add_settings_provider(from_linter)
    service.add_settings_provider(from_formatter)

    await service.ensure_started("file:///project", _META)

    assert stub.settings_at_start == {
        "configuration": {
            "target-version": "py311",
            "format": {"quote-style": "single"},
        }
    }


async def test_providers_run_once_no_matter_how_many_handlers_arrive_at_once() -> None:
    # deriving the language level runs another action; concurrent lint and format runs
    # must not each pay for it, nor race to half-configure the server
    service, _, _ = _service()
    calls = 0

    async def provider(meta: code_action.RunActionMeta) -> dict[str, typing.Any]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return {"lineLength": 100}

    service.add_settings_provider(provider)

    await asyncio.gather(
        service.ensure_started("file:///project", _META),
        service.ensure_started("file:///project", _META),
        service.ensure_started("file:///project", _META),
    )

    assert calls == 1


async def test_a_provider_registered_too_late_is_reported_rather_than_dropped_quietly() -> (
    None
):
    # the symptom otherwise is a handler's whole configuration silently not applying
    service, stub, logger = _service()

    async def early(meta: code_action.RunActionMeta) -> dict[str, typing.Any]:
        return {"lineLength": 88}

    async def late(meta: code_action.RunActionMeta) -> dict[str, typing.Any]:
        return {"lint": {"extendSelect": ["B"]}}

    service.add_settings_provider(early)
    await service.ensure_started("file:///project", _META)
    service.add_settings_provider(late)
    await service.ensure_started("file:///project", _META)

    assert stub.settings_at_start == {"lineLength": 88}
    assert any("will not apply" in warning for warning in logger.warnings)


def test_code_action_edits_are_not_deferred_to_a_resolve_round_trip() -> None:
    """Ruff must answer code-action requests with the edits included.

    Told that the client will fetch edits separately, ruff returns actions
    carrying none. An empty edit set is a legal fix — display-only fixes exist —
    so those arrive as fixes that look applicable, are offered to the user, and
    change nothing when applied, with no error anywhere to explain it.
    """
    code_action = _RUFF_CLIENT_CAPABILITIES["textDocument"]["codeAction"]

    assert "resolveSupport" not in code_action
    assert "dataSupport" not in code_action
