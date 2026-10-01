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
import sys
from pathlib import Path

import psutil  # type: ignore[import-untyped]

__all__ = [
    "CgroupMemory",
    "HostPressure",
    "MemInfo",
    "read_cgroup_memory",
    "read_host_pressure",
    "read_meminfo",
]


@dataclasses.dataclass(frozen=True)
class HostPressure:
    """A snapshot of the host's memory and IO pressure, in the units of the logs."""

    mem_available_mb: int | None
    swap_used_mb: int | None
    psi_memory_full_avg10: float | None
    psi_io_full_avg10: float | None
    psi_cpu_some_avg10: float | None

    def describe(self) -> str:
        return (
            f"memory available={_fmt_mb(self.mem_available_mb)}"
            f" swap used={_fmt_mb(self.swap_used_mb)}"
            f" PSI memory full={_fmt_pct(self.psi_memory_full_avg10)}"
            f" io full={_fmt_pct(self.psi_io_full_avg10)}"
            f" cpu some={_fmt_pct(self.psi_cpu_some_avg10)}"
        )

    def fields(self) -> dict[str, int | float | None]:
        return {
            "mem_available_mb": self.mem_available_mb,
            "swap_used_mb": self.swap_used_mb,
            "psi_memory_full_avg10": self.psi_memory_full_avg10,
            "psi_io_full_avg10": self.psi_io_full_avg10,
            "psi_cpu_some_avg10": self.psi_cpu_some_avg10,
        }


def _fmt_mb(value: int | None) -> str:
    return "n/a" if value is None else f"{value}MB"


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}%"


@dataclasses.dataclass(frozen=True)
class MemInfo:
    mem_total_mb: int | None
    mem_available_mb: int | None
    swap_total_mb: int | None
    swap_used_mb: int | None


@dataclasses.dataclass(frozen=True)
class CgroupMemory:
    memory_max_mb: int | None
    memory_current_mb: int | None
    swap_current_mb: int | None


def _meminfo_values_kb(text: str) -> dict[str, int]:
    values_kb: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].endswith(":"):
            try:
                values_kb[parts[0][:-1]] = int(parts[1])
            except ValueError:
                continue
    return values_kb


def read_meminfo(platform: str = sys.platform) -> MemInfo:
    if platform.startswith("linux"):
        try:
            with open("/proc/meminfo", encoding="utf-8") as meminfo_file:
                values_kb = _meminfo_values_kb(meminfo_file.read())
        except OSError:
            return MemInfo(
                mem_total_mb=None,
                mem_available_mb=None,
                swap_total_mb=None,
                swap_used_mb=None,
            )
        mem_total_kb = values_kb.get("MemTotal")
        mem_available_kb = values_kb.get("MemAvailable")
        swap_total_kb = values_kb.get("SwapTotal")
        swap_free_kb = values_kb.get("SwapFree")
        swap_used_mb: int | None = None
        if swap_total_kb is not None and swap_free_kb is not None:
            swap_used_mb = max(0, swap_total_kb - swap_free_kb) // 1024
        return MemInfo(
            mem_total_mb=None if mem_total_kb is None else mem_total_kb // 1024,
            mem_available_mb=None
            if mem_available_kb is None
            else mem_available_kb // 1024,
            swap_total_mb=None if swap_total_kb is None else swap_total_kb // 1024,
            swap_used_mb=swap_used_mb,
        )
    try:
        virtual = psutil.virtual_memory()
        swap = psutil.swap_memory()
        return MemInfo(
            mem_total_mb=int(virtual.total) // (1024 * 1024),
            mem_available_mb=int(virtual.available) // (1024 * 1024),
            swap_total_mb=int(swap.total) // (1024 * 1024),
            swap_used_mb=int(swap.used) // (1024 * 1024),
        )
    except Exception:
        return MemInfo(
            mem_total_mb=None,
            mem_available_mb=None,
            swap_total_mb=None,
            swap_used_mb=None,
        )


def read_cgroup_memory(
    proc_root: Path = Path("/proc"), cgroup_root: Path = Path("/sys/fs/cgroup")
) -> CgroupMemory | None:
    try:
        cgroup_text = (proc_root / "self" / "cgroup").read_text(encoding="utf-8")
    except OSError:
        return None
    leaf: str | None = None
    for line in cgroup_text.splitlines():
        if line.startswith("0::"):
            leaf = line[len("0::") :].strip() or "/"
            break
    if leaf is None:
        return None
    leaf_dir = cgroup_root / leaf.lstrip("/")
    try:
        current_text = (leaf_dir / "memory.current").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        memory_current_mb = int(current_text.strip()) // (1024 * 1024)
    except ValueError:
        return None
    memory_max_mb: int | None = None
    try:
        max_text = (leaf_dir / "memory.max").read_text(encoding="utf-8").strip()
    except OSError:
        max_text = "max"
    if max_text != "max":
        try:
            memory_max_mb = int(max_text) // (1024 * 1024)
        except ValueError:
            memory_max_mb = None
    try:
        swap_text = (leaf_dir / "memory.swap.current").read_text(encoding="utf-8")
        swap_current_mb: int | None = int(swap_text.strip()) // (1024 * 1024)
    except (OSError, ValueError):
        swap_current_mb = None
    return CgroupMemory(
        memory_max_mb=memory_max_mb,
        memory_current_mb=memory_current_mb,
        swap_current_mb=swap_current_mb,
    )


def _parse_meminfo(text: str) -> tuple[int | None, int | None]:
    """(MemAvailable MB, swap used MB) from ``/proc/meminfo`` contents.

    Swap used is ``SwapTotal - SwapFree``: the file reports neither figure as
    "used", and the pair is what says whether earlier output is being paged out.
    """
    values_kb = _meminfo_values_kb(text)

    mem_available_kb = values_kb.get("MemAvailable")
    mem_available_mb = None if mem_available_kb is None else mem_available_kb // 1024

    swap_total_kb = values_kb.get("SwapTotal")
    swap_free_kb = values_kb.get("SwapFree")
    swap_used_mb: int | None = None
    if swap_total_kb is not None and swap_free_kb is not None:
        swap_used_mb = max(0, swap_total_kb - swap_free_kb) // 1024

    return mem_available_mb, swap_used_mb


def _parse_psi(text: str) -> tuple[float | None, float | None]:
    """(some avg10, full avg10) from a ``/proc/pressure/*`` file."""
    some_avg10: float | None = None
    full_avg10: float | None = None
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        avg10_value: float | None = None
        for part in parts[1:]:
            if part.startswith("avg10="):
                avg10_value = float(part.removeprefix("avg10="))
                break
        if parts[0] == "some":
            some_avg10 = avg10_value
        elif parts[0] == "full":
            full_avg10 = avg10_value
    return some_avg10, full_avg10


def read_host_pressure() -> HostPressure:
    mem_available_mb: int | None = None
    swap_used_mb: int | None = None
    try:
        with open("/proc/meminfo") as meminfo_file:
            mem_available_mb, swap_used_mb = _parse_meminfo(meminfo_file.read())
    except OSError:
        pass

    psi_memory_full: float | None = None
    try:
        with open("/proc/pressure/memory") as memory_psi_file:
            _, psi_memory_full = _parse_psi(memory_psi_file.read())
    except OSError:
        pass

    psi_io_full: float | None = None
    try:
        with open("/proc/pressure/io") as io_psi_file:
            _, psi_io_full = _parse_psi(io_psi_file.read())
    except OSError:
        pass

    psi_cpu_some: float | None = None
    try:
        with open("/proc/pressure/cpu") as cpu_psi_file:
            psi_cpu_some, _ = _parse_psi(cpu_psi_file.read())
    except OSError:
        pass

    return HostPressure(
        mem_available_mb=mem_available_mb,
        swap_used_mb=swap_used_mb,
        psi_memory_full_avg10=psi_memory_full,
        psi_io_full_avg10=psi_io_full,
        psi_cpu_some_avg10=psi_cpu_some,
    )
