"""Runtime resource guard for QA pipeline runs."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass


class ResourceGuardError(RuntimeError):
    """Raised when the host remains overloaded beyond the configured wait."""


@dataclass
class ResourceSnapshot:
    cpu_count: int
    load_1m: float
    load_threshold: float
    mem_available_gb: float | None
    min_free_mem_gb: float

    @property
    def overloaded(self) -> bool:
        if self.load_1m > self.load_threshold:
            return True
        if self.mem_available_gb is not None and self.mem_available_gb < self.min_free_mem_gb:
            return True
        return False

    def reason(self) -> str:
        parts = [
            f"load_1m={self.load_1m:.2f}",
            f"load_threshold={self.load_threshold:.2f}",
        ]
        if self.mem_available_gb is not None:
            parts.extend(
                [
                    f"mem_available_gb={self.mem_available_gb:.2f}",
                    f"min_free_mem_gb={self.min_free_mem_gb:.2f}",
                ]
            )
        return ", ".join(parts)


class ResourceGuard:
    """Protect a small server from accidental over-parallel QA runs."""

    def __init__(
        self,
        enabled: bool = True,
        max_load_ratio: float = 0.75,
        min_free_mem_gb: float = 3.0,
        check_interval_seconds: float = 30.0,
        max_wait_seconds: float = 120.0,
        overload_action: str = "pause",
        max_workers_safe: int | None = None,
    ) -> None:
        self.enabled = enabled
        self.cpu_count = os.cpu_count() or 1
        self.max_load_ratio = max_load_ratio
        self.min_free_mem_gb = min_free_mem_gb
        self.check_interval_seconds = check_interval_seconds
        self.max_wait_seconds = max_wait_seconds
        self.overload_action = overload_action
        self.max_workers_safe = max_workers_safe or max(1, self.cpu_count // 2)
        self._last_check_time = 0.0

    def effective_workers(self, requested_workers: int) -> int:
        requested = max(1, int(requested_workers))
        if not self.enabled:
            return requested
        effective = min(requested, self.max_workers_safe)
        if effective < requested:
            print(
                "Resource guard: reducing workers from "
                f"{requested} to {effective} on {self.cpu_count}-core host."
            )
        return effective

    def wait_if_needed(self, label: str, force: bool = False) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if not force and now - self._last_check_time < self.check_interval_seconds:
            return
        self._last_check_time = now
        snapshot = self.snapshot()
        if not snapshot.overloaded:
            return
        message = f"Resource guard: host overloaded during {label}: {snapshot.reason()}"
        if self.overload_action == "stop":
            raise ResourceGuardError(message)
        print()
        print(message)
        print(f"Resource guard: pausing for up to {self.max_wait_seconds:.0f}s...")
        start = time.monotonic()
        while time.monotonic() - start < self.max_wait_seconds:
            time.sleep(min(10.0, max(1.0, self.max_wait_seconds - (time.monotonic() - start))))
            snapshot = self.snapshot()
            if not snapshot.overloaded:
                print("Resource guard: resources recovered; resuming.")
                self._last_check_time = time.monotonic()
                return
        raise ResourceGuardError(
            "Resource guard: host remained overloaded for "
            f"{self.max_wait_seconds:.0f}s during {label}: {snapshot.reason()}"
        )

    def snapshot(self) -> ResourceSnapshot:
        try:
            load_1m = os.getloadavg()[0]
        except OSError:
            load_1m = 0.0
        return ResourceSnapshot(
            cpu_count=self.cpu_count,
            load_1m=load_1m,
            load_threshold=self.cpu_count * self.max_load_ratio,
            mem_available_gb=_mem_available_gb(),
            min_free_mem_gb=self.min_free_mem_gb,
        )


def _mem_available_gb() -> float | None:
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) / 1024 / 1024
    except OSError:
        return None
    return None
