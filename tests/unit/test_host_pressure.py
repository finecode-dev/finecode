"""Requirement tests (R4): host memory/IO pressure must be readable for a diagnostic.

REQUIREMENT: a stalled ER boot is often a host-pressure symptom, not a bug in
the process being started. Reading ``/proc/meminfo`` and ``/proc/pressure/*``
turns a bare timeout into an attribution. The reads must be total: a missing or
unparsable file yields ``None`` fields rather than replacing the error that the
diagnostic is explaining.
"""

from __future__ import annotations

from finecode.wm_server import host_pressure


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
