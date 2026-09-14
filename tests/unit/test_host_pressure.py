"""Requirement tests (R4): host memory/IO pressure must be readable for a diagnostic.

REQUIREMENT: a stalled ER boot is often a host-pressure symptom, not a bug in
the process being started. Reading ``/proc/meminfo`` and ``/proc/pressure/*``
turns a bare timeout into an attribution. The reads must be total: a missing or
unparsable file yields ``None`` fields rather than replacing the error that the
diagnostic is explaining.
"""

from __future__ import annotations

import pytest

from finecode.wm_server import host_pressure

_MEMINFO_SAMPLE = """MemTotal:       16299560 kB
MemFree:          123456 kB
MemAvailable:    4916544 kB
SwapTotal:      20736000 kB
SwapFree:        2925760 kB
"""

_PSI_SAMPLE = """some avg10=0.18 avg60=0.21 avg300=0.34 total=12345
full avg10=0.27 avg60=0.30 avg300=0.40 total=6789
"""


def test_parse_meminfo_computes_available_and_used_swap() -> None:
    mem_available_mb, swap_used_mb = host_pressure._parse_meminfo(_MEMINFO_SAMPLE)

    assert mem_available_mb == 4801
    assert swap_used_mb == 17392


def test_parse_psi_reads_some_and_full_avg10() -> None:
    some_avg10, full_avg10 = host_pressure._parse_psi(_PSI_SAMPLE)

    assert some_avg10 == 0.18
    assert full_avg10 == 0.27


def test_describe_renders_none_as_not_available() -> None:
    pressure = host_pressure.HostPressure(
        mem_available_mb=None,
        swap_used_mb=None,
        psi_memory_full_avg10=None,
        psi_io_full_avg10=None,
        psi_cpu_some_avg10=None,
    )

    described = pressure.describe()

    assert "memory available=n/a" in described
    assert "swap used=n/a" in described
    assert "PSI memory full=n/a" in described


def test_read_host_pressure_is_none_when_files_are_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-Linux hosts and containers without PSI have no such files; the
    diagnostic must degrade to ``None``, not fail."""

    def _raise(*_args, **_kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr("builtins.open", _raise)

    pressure = host_pressure.read_host_pressure()

    assert pressure.mem_available_mb is None
    assert pressure.swap_used_mb is None
    assert pressure.psi_memory_full_avg10 is None
    assert pressure.psi_io_full_avg10 is None
    assert pressure.psi_cpu_some_avg10 is None
    assert "n/a" in pressure.describe()


def test_fields_expose_every_value_for_structured_logging() -> None:
    pressure = host_pressure.HostPressure(
        mem_available_mb=4801,
        swap_used_mb=17821,
        psi_memory_full_avg10=0.27,
        psi_io_full_avg10=1.25,
        psi_cpu_some_avg10=0.18,
    )

    fields = pressure.fields()

    assert fields["mem_available_mb"] == 4801
    assert fields["swap_used_mb"] == 17821
    assert fields["psi_memory_full_avg10"] == 0.27
    assert fields["psi_io_full_avg10"] == 1.25
    assert fields["psi_cpu_some_avg10"] == 0.18
