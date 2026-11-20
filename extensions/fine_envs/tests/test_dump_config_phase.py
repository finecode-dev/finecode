"""The config-dump render is timed as one synchronous phase.

The span must show elapsed and thread CPU separately so a slow render can be
told apart from waiting on the provider, and its counts must match the actual
dump, selection and output. Without this, a slow ``dump_config`` leaves no
evidence of where its time went.
"""

from __future__ import annotations

import asyncio
import collections.abc
import threading
import time
import types
import typing
from typing import Any

import pytest

from fine_envs.dump_config_handler import DumpConfigHandler


@pytest.fixture()
def telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> collections.abc.Iterator[tuple[object, object]]:
    """The runner timing runtime wired to an in-memory exporter."""
    from finecode_extension_api.phase_timing_runtime import set_phase_timing_binding
    from finecode_extension_runner.shared_telemetry.health import HealthTracker
    from finecode_extension_runner.shared_telemetry.phase_spans import (
        OtelPhaseTimingBinding,
    )
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(
        otel_trace,
        "get_tracer",
        lambda name, *args, **kwargs: provider.get_tracer(name),
    )
    binding = OtelPhaseTimingBinding(health=HealthTracker(process_role="er"))
    set_phase_timing_binding(binding)
    try:
        yield binding, exporter
    finally:
        set_phase_timing_binding(None)


class _Provider:
    def __init__(self, selection: dict[str, list[str]], delay_sec: float = 0.0) -> None:
        self._selection = selection
        self._delay = delay_sec

    async def get_workspace_extra_selection(self) -> dict[str, list[str]]:
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._selection


def _handler(
    selection: dict[str, list[str]], delay_sec: float = 0.0
) -> DumpConfigHandler:
    return DumpConfigHandler(
        project_info_provider=typing.cast(typing.Any, _Provider(selection, delay_sec))
    )


def _context(raw_config: dict) -> types.SimpleNamespace:
    return types.SimpleNamespace(raw_config_dump=raw_config, config_dump_content=None)


def _phase_attrs(exporter) -> dict[str, Any]:
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    return dict(spans[0].attributes or {})


def _cpu_and_elapsed(attrs: dict) -> tuple[int, int]:
    return int(attrs["finecode.phase.thread_cpu_ns"]), int(
        attrs["finecode.phase.elapsed_ns"]
    )


async def test_cpu_bound_render_attributes_cpu_separately(
    telemetry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CPU-bound render shows CPU near elapsed: the time was burnt here."""
    import fine_envs.dump_config_handler as handler_module

    _binding, exporter = telemetry

    def _burn(_raw: dict, _selection: dict[str, list[str]]) -> str:
        stop_at = time.perf_counter() + 0.3
        while time.perf_counter() < stop_at:
            pass
        return "x" * 10

    raw = {"tool": {"finecode": {}}, "project": {"name": "p"}}
    monkeypatch.setattr(handler_module, "render_config_dump", _burn)
    await _handler({}).run(typing.cast(typing.Any, None), _context(raw))

    attrs = _phase_attrs(exporter)
    cpu_ns, elapsed_ns = _cpu_and_elapsed(attrs)
    assert elapsed_ns >= 250_000_000
    assert cpu_ns >= 0.8 * elapsed_ns


async def test_getter_wait_stays_outside_the_phase(telemetry) -> None:
    """A slow provider delays the action but not the phase: the getter wait
    happens before the scope opens."""
    _binding, exporter = telemetry
    raw = {"tool": {"finecode": {}}}
    await _handler({}, delay_sec=0.3).run(typing.cast(typing.Any, None), _context(raw))

    attrs = _phase_attrs(exporter)
    _cpu_ns, elapsed_ns = _cpu_and_elapsed(attrs)
    assert elapsed_ns < 200_000_000


async def test_sibling_cpu_thread_does_not_inflate_cpu(
    telemetry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Thread CPU covers only the render thread: a busy sibling lengthens the
    wall clock, never the CPU reading."""
    import fine_envs.dump_config_handler as handler_module

    _binding, exporter = telemetry
    stop_sibling = threading.Event()

    def _sibling() -> None:
        while not stop_sibling.is_set():
            pass

    def _burn(_raw: dict, _selection: dict[str, list[str]]) -> str:
        stop_at = time.perf_counter() + 0.2
        while time.perf_counter() < stop_at:
            pass
        return "y" * 5

    raw = {"tool": {"finecode": {}}}
    monkeypatch.setattr(handler_module, "render_config_dump", _burn)
    thread = threading.Thread(target=_sibling)
    thread.start()
    try:
        await _handler({}).run(typing.cast(typing.Any, None), _context(raw))
    finally:
        stop_sibling.set()
        thread.join()

    attrs = _phase_attrs(exporter)
    cpu_ns, elapsed_ns = _cpu_and_elapsed(attrs)
    assert cpu_ns <= elapsed_ns


async def test_raising_renderer_keeps_exception_and_marks_completion_unavailable(
    telemetry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing render keeps its exception and records an unavailable output."""
    import fine_envs.dump_config_handler as handler_module

    _binding, exporter = telemetry
    marker = ValueError("render boom")

    def _raise(_raw: dict, _selection: dict[str, list[str]]) -> str:
        raise marker

    raw = {"tool": {"finecode": {}}}
    monkeypatch.setattr(handler_module, "render_config_dump", _raise)
    with pytest.raises(ValueError) as exc_info:
        await _handler({}).run(typing.cast(typing.Any, None), _context(raw))
    assert exc_info.value is marker
    attrs = _phase_attrs(exporter)
    assert attrs["finecode.phase.outcome"] == "raised"
    assert attrs["finecode.phase.completion"] == "unavailable"
    assert "finecode.phase.count.output_chars" not in attrs


async def test_counts_match_dump_selection_and_output(telemetry) -> None:
    """Counts equal the actual keys, selection entries and output length."""
    from fine_envs.dump_config_render import render_config_dump

    _binding, exporter = telemetry
    selection = {"dep-a": ["e1", "e2"], "dep-b": ["e3"]}
    raw = {
        "top": {"a": 1},
        "tool": {"finecode": {"presets": [{"source": "x"}], "extra": {"k": 1}}},
        "project": {"name": "p"},
    }
    context = _context(raw)
    await _handler(selection).run(typing.cast(typing.Any, None), context)

    content = context.config_dump_content
    assert content == render_config_dump(raw, selection)
    attrs = _phase_attrs(exporter)
    assert attrs["finecode.phase.count.top_level_keys"] == 3
    assert attrs["finecode.phase.count.selected_dependencies"] == 2
    assert attrs["finecode.phase.count.selected_extras"] == 3
    assert attrs["finecode.phase.count.output_chars"] == len(content)
