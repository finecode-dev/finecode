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


def test_memory_pressure_psi_boundary() -> None:
    """A host at the PSI limit must read as pressured, just below it as fine.

    The threshold decides whether a run failure carries a host note, so an
    off-by-one here either spams healthy hosts or hides the attribution.
    """
    below = host_pressure.memory_pressure_reasons(
        psi_memory_full_avg10=9.99,
        mem_total_mb=17920,
        mem_available_mb=10000,
        swap_total_mb=20728,
        swap_used_mb=0,
    )
    at = host_pressure.memory_pressure_reasons(
        psi_memory_full_avg10=10.0,
        mem_total_mb=17920,
        mem_available_mb=10000,
        swap_total_mb=20728,
        swap_used_mb=0,
    )

    assert below == ()
    assert at == ("psi",)


def test_memory_pressure_memory_exhausted_boundaries() -> None:
    """Memory exhaustion needs both little available memory and full swap.

    Swap alone fires on healthy hosts that merely left cold pages paged out,
    so the conjunct keeps the run-failure note from crying wolf.
    """
    base = {
        "psi_memory_full_avg10": 0.0,
        "mem_total_mb": 20000,
        "mem_available_mb": 1000,
        "swap_total_mb": 10000,
        "swap_used_mb": 9000,
    }
    assert host_pressure.memory_pressure_reasons(**base) == ("memoryExhausted",)

    just_over = dict(base, mem_available_mb=1002)
    assert host_pressure.memory_pressure_reasons(**just_over) == ()

    swap_just_under = dict(base, swap_used_mb=8990)
    assert host_pressure.memory_pressure_reasons(**swap_just_under) == ()

    no_swap = dict(base, swap_total_mb=0, swap_used_mb=0)
    assert host_pressure.memory_pressure_reasons(**no_swap) == ("memoryExhausted",)

    unknown_swap = dict(base, swap_total_mb=None, swap_used_mb=None)
    assert host_pressure.memory_pressure_reasons(**unknown_swap) == ()


def test_memory_pressure_not_evaluable_without_any_input() -> None:
    """With nothing to judge by the predicate must abstain, not claim calm.

    An empty tuple would let callers report "no pressure" on hosts where the
    files are simply absent; ``None`` keeps those hosts out of the verdict.
    """
    assert (
        host_pressure.memory_pressure_reasons(
            psi_memory_full_avg10=None,
            mem_total_mb=None,
            mem_available_mb=None,
            swap_total_mb=None,
            swap_used_mb=None,
        )
        is None
    )

    known_memory = host_pressure.memory_pressure_reasons(
        psi_memory_full_avg10=None,
        mem_total_mb=17920,
        mem_available_mb=10000,
        swap_total_mb=20728,
        swap_used_mb=0,
    )
    assert isinstance(known_memory, tuple)


def test_memory_pressure_healthy_host_with_swap_left_over() -> None:
    """Cold pages left in swap must not read as pressure on a healthy host."""
    assert (
        host_pressure.memory_pressure_reasons(
            psi_memory_full_avg10=0.0,
            mem_total_mb=17920,
            mem_available_mb=11059,
            swap_total_mb=20684,
            swap_used_mb=8806,
        )
        == ()
    )


def test_memory_pressure_incident_values_trip_both_reasons() -> None:
    """The values from the observed incident must trip every reason."""
    assert host_pressure.memory_pressure_reasons(
        psi_memory_full_avg10=76.91,
        mem_total_mb=17920,
        mem_available_mb=545,
        swap_total_mb=20728,
        swap_used_mb=20727,
    ) == ("psi", "memoryExhausted")


def test_read_psi_memory_full_avg10_from_proc_root(tmp_path) -> None:
    """The sampler reads one PSI file so a missing host file degrades to ``None``."""
    pressure_dir = tmp_path / "pressure"
    pressure_dir.mkdir(parents=True)
    (pressure_dir / "memory").write_text(
        "some avg10=0.18 avg60=0.21 avg300=0.34 total=12345\n"
        "full avg10=76.91 avg60=0.30 avg300=0.40 total=6789\n",
        encoding="utf-8",
    )

    assert host_pressure.read_psi_memory_full_avg10(tmp_path) == 76.91
    assert host_pressure.read_psi_memory_full_avg10(tmp_path / "missing") is None

    (pressure_dir / "memory").write_text("not a psi file\n", encoding="utf-8")
    assert host_pressure.read_psi_memory_full_avg10(tmp_path) is None


def test_read_memory_pressure_combines_meminfo_and_pressure(monkeypatch) -> None:
    """The failure note needs one call that works off Linux too."""
    monkeypatch.setattr(
        host_pressure,
        "read_meminfo",
        lambda: host_pressure.MemInfo(
            mem_total_mb=17920,
            mem_available_mb=545,
            swap_total_mb=20728,
            swap_used_mb=20727,
        ),
    )
    monkeypatch.setattr(
        host_pressure,
        "read_host_pressure",
        lambda: host_pressure.HostPressure(
            mem_available_mb=545,
            swap_used_mb=20727,
            psi_memory_full_avg10=76.91,
            psi_io_full_avg10=1.25,
            psi_cpu_some_avg10=0.18,
        ),
    )

    reading = host_pressure.read_memory_pressure()

    assert reading.active is True
    assert reading.reasons == ("psi", "memoryExhausted")
    fields = reading.fields()
    assert fields == {
        "mem_total_mb": 17920,
        "mem_available_mb": 545,
        "swap_total_mb": 20728,
        "swap_used_mb": 20727,
        "psi_memory_full_avg10": 76.91,
        "psi_io_full_avg10": 1.25,
        "psi_cpu_some_avg10": 0.18,
        "memory_pressure": True,
    }
    assert reading.describe() == (
        "memory available=545MB of 17920MB"
        " swap used=20727MB of 20728MB"
        " PSI memory full=76.91%"
    )
