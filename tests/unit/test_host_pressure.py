"""Requirement tests (R4): host memory/IO pressure must be readable for a diagnostic.

REQUIREMENT: a stalled ER boot is often a host-pressure symptom, not a bug in
the process being started. Reading ``/proc/meminfo`` and ``/proc/pressure/*``
turns a bare timeout into an attribution. The reads must be total: a missing or
unparsable file yields ``None`` fields rather than replacing the error that the
diagnostic is explaining.
"""

from __future__ import annotations

import types

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


def test_meminfo_values_include_totals() -> None:
    values = host_pressure._meminfo_values_kb(_MEMINFO_SAMPLE)

    assert values["MemTotal"] == 16299560
    assert values["SwapTotal"] == 20736000


def test_read_meminfo_off_linux_uses_psutil(monkeypatch) -> None:
    """Off Linux there is no meminfo, so the snapshot uses psutil.

    Without it the snapshot would report nulls on exactly the machines
    where developers most often read it.
    """
    fake_psutil = types.SimpleNamespace(
        virtual_memory=lambda: types.SimpleNamespace(
            total=32 * 1024**3, available=4 * 1024**3
        ),
        swap_memory=lambda: types.SimpleNamespace(
            total=20 * 1024**3, used=12 * 1024**3
        ),
    )
    monkeypatch.setattr(host_pressure, "psutil", fake_psutil)

    info = host_pressure.read_meminfo(platform="darwin")

    assert info.mem_total_mb == 32768
    assert info.mem_available_mb == 4096
    assert info.swap_total_mb == 20480
    assert info.swap_used_mb == 12288


def test_read_cgroup_memory_max_limit_and_swap(tmp_path) -> None:
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    (proc_root / "self").mkdir(parents=True)
    (proc_root / "self" / "cgroup").write_text("0::/user.slice\n", encoding="utf-8")
    leaf = cgroup_root / "user.slice"
    leaf.mkdir(parents=True)
    (leaf / "memory.current").write_text(str(947 * 1024 * 1024), encoding="utf-8")
    (leaf / "memory.max").write_text("max", encoding="utf-8")
    (leaf / "memory.swap.current").write_text(str(270 * 1024 * 1024), encoding="utf-8")

    result = host_pressure.read_cgroup_memory(
        proc_root=proc_root, cgroup_root=cgroup_root
    )

    assert result is not None
    assert result.memory_max_mb is None
    assert result.memory_current_mb == 947
    assert result.swap_current_mb == 270


def test_read_cgroup_memory_numeric_limit(tmp_path) -> None:
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    cgroup_root.mkdir(parents=True)
    (proc_root / "self").mkdir(parents=True)
    (proc_root / "self" / "cgroup").write_text("0::/\n", encoding="utf-8")
    (cgroup_root / "memory.current").write_text(
        str(100 * 1024 * 1024), encoding="utf-8"
    )
    (cgroup_root / "memory.max").write_text(str(16 * 1024**3), encoding="utf-8")

    result = host_pressure.read_cgroup_memory(
        proc_root=proc_root, cgroup_root=cgroup_root
    )

    assert result is not None
    assert result.memory_max_mb == 16384
    assert result.memory_current_mb == 100


def test_read_cgroup_memory_missing_swap_and_no_cgroup_line(tmp_path) -> None:
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    cgroup_root.mkdir(parents=True)
    (proc_root / "self").mkdir(parents=True)
    (proc_root / "self" / "cgroup").write_text("0::/\n", encoding="utf-8")
    (cgroup_root / "memory.current").write_text(str(10 * 1024 * 1024), encoding="utf-8")
    (cgroup_root / "memory.max").write_text("max", encoding="utf-8")

    result = host_pressure.read_cgroup_memory(
        proc_root=proc_root, cgroup_root=cgroup_root
    )

    assert result is not None
    assert result.swap_current_mb is None

    (proc_root / "self" / "cgroup").write_text("1:name=systemd:/\n", encoding="utf-8")
    assert (
        host_pressure.read_cgroup_memory(proc_root=proc_root, cgroup_root=cgroup_root)
        is None
    )
