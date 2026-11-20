# docs: docs/guides/wm-server-internals.md
"""Host memory and IO pressure, read from ``/proc``.

A stalled process (an ER that never publishes its port, a loop that lags) is
frequently a *host* problem rather than the process's own: the machine is out of
memory, the page cache is being thrashed, or IO is saturated. Those facts are
not visible from inside the stalled process, so a diagnostic that reports them
beside the stall turns a bare timeout into an attribution.

Every field is ``None`` when the kernel does not expose it (non-Linux hosts,
older kernels, some containers). This module is diagnostic only; nothing depends
on it being accurate or even present.
"""

from __future__ import annotations

import dataclasses

from finecode_common.host_readers import (
    CgroupMemory,
    HostPressure,
    MemInfo,
    read_cgroup_memory,
    read_host_pressure,
    read_meminfo,
    read_psi_memory_full_avg10,
)

__all__ = [
    "CgroupMemory",
    "HostPressure",
    "MemInfo",
    "MemoryPressureReading",
    "memory_pressure_reasons",
    "read_cgroup_memory",
    "read_host_pressure",
    "read_meminfo",
    "read_memory_pressure",
    "read_psi_memory_full_avg10",
]

MEMORY_PRESSURE_PSI_FULL_PCT = 10.0
MEMORY_PRESSURE_AVAILABLE_PCT = 5.0
MEMORY_PRESSURE_SWAP_USED_PCT = 90.0


def memory_pressure_reasons(
    *,
    psi_memory_full_avg10: float | None,
    mem_total_mb: int | None,
    mem_available_mb: int | None,
    swap_total_mb: int | None,
    swap_used_mb: int | None,
) -> tuple[str, ...] | None:
    """Reasons the host counts as under memory pressure, or evaluability.

    Returns ``None`` when the predicate cannot be evaluated (no PSI and no
    usable memory figures); otherwise a tuple of reasons, empty when the host
    is not pressured. Host-wide ``/proc`` figures only: a container thrashing
    against its own cgroup limit while the host is fine does not trip this.
    """
    memory_known = (
        mem_total_mb is not None and mem_available_mb is not None and mem_total_mb > 0
    )
    if psi_memory_full_avg10 is None and not memory_known:
        return None
    reasons: list[str] = []
    if (
        psi_memory_full_avg10 is not None
        and psi_memory_full_avg10 >= MEMORY_PRESSURE_PSI_FULL_PCT
    ):
        reasons.append("psi")
    memory_exhausted = False
    if memory_known:
        assert mem_total_mb is not None and mem_available_mb is not None
        if mem_available_mb * 100 <= MEMORY_PRESSURE_AVAILABLE_PCT * mem_total_mb:
            swap_pressure = swap_total_mb == 0 or (
                swap_total_mb is not None
                and swap_total_mb > 0
                and swap_used_mb is not None
                and swap_used_mb * 100 >= MEMORY_PRESSURE_SWAP_USED_PCT * swap_total_mb
            )
            if swap_pressure:
                memory_exhausted = True
    if memory_exhausted:
        reasons.append("memoryExhausted")
    return tuple(reasons)


@dataclasses.dataclass(frozen=True)
class MemoryPressureReading:
    """One joint reading of host memory figures and pressure."""

    meminfo: MemInfo
    pressure: HostPressure
    reasons: tuple[str, ...] | None

    @property
    def active(self) -> bool:
        return bool(self.reasons)

    def fields(self) -> dict[str, int | float | bool | None]:
        return {
            "mem_total_mb": self.meminfo.mem_total_mb,
            "mem_available_mb": self.meminfo.mem_available_mb,
            "swap_total_mb": self.meminfo.swap_total_mb,
            "swap_used_mb": self.meminfo.swap_used_mb,
            "psi_memory_full_avg10": self.pressure.psi_memory_full_avg10,
            "psi_io_full_avg10": self.pressure.psi_io_full_avg10,
            "psi_cpu_some_avg10": self.pressure.psi_cpu_some_avg10,
            "memory_pressure": self.active,
        }

    def describe(self) -> str:
        return (
            f"memory available={_fmt_mb(self.meminfo.mem_available_mb)}"
            f" of {_fmt_mb(self.meminfo.mem_total_mb)}"
            f" swap used={_fmt_mb(self.meminfo.swap_used_mb)}"
            f" of {_fmt_mb(self.meminfo.swap_total_mb)}"
            f" PSI memory full={_fmt_pct(self.pressure.psi_memory_full_avg10)}"
        )


def _fmt_mb(value: int | None) -> str:
    return "n/a" if value is None else f"{value}MB"


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}%"


def read_memory_pressure() -> MemoryPressureReading:
    meminfo = read_meminfo()
    pressure = read_host_pressure()
    reasons = memory_pressure_reasons(
        psi_memory_full_avg10=pressure.psi_memory_full_avg10,
        mem_total_mb=meminfo.mem_total_mb,
        mem_available_mb=meminfo.mem_available_mb,
        swap_total_mb=meminfo.swap_total_mb,
        swap_used_mb=meminfo.swap_used_mb,
    )
    return MemoryPressureReading(meminfo=meminfo, pressure=pressure, reasons=reasons)
