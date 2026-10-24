"""Feedback-controlled, low-priority CPU and resident-memory load for Linux."""

from __future__ import annotations

import dataclasses
import datetime
import json
import logging
import math
import mmap
import multiprocessing
import os
import signal
import time
from pathlib import Path

MIB = 1024 * 1024
GIB = 1024 * MIB
LOGGER = logging.getLogger("resource_guard")


@dataclasses.dataclass(frozen=True)
class Config:
    cpu_target: float = 0.25
    memory_target: float = 0.25
    sample_seconds: float = 2.0
    growth_bytes: int = 128 * MIB
    reserve_bytes: int = 512 * MIB
    max_ballast_bytes: int = 3584 * MIB
    max_ballast_fraction: float = 0.30
    max_cpu_duty: float = 0.40
    chunk_bytes: int = 8 * MIB

    def __post_init__(self):
        for value in (self.cpu_target, self.memory_target, self.max_ballast_fraction, self.max_cpu_duty):
            if not math.isfinite(value) or not 0 < value <= 1:
                raise ValueError("Resource fractions must be finite and between zero and one")
        if not math.isfinite(self.sample_seconds) or self.sample_seconds <= 0:
            raise ValueError("The sample interval must be positive and finite")
        if self.reserve_bytes < 0 or self.max_ballast_bytes < 0:
            raise ValueError("Memory limits cannot be negative")
        if self.growth_bytes <= 0 or self.chunk_bytes <= 0:
            raise ValueError("Memory allocation steps must be positive")

    @classmethod
    def from_environment(cls):
        return cls(
            cpu_target=float(os.environ.get("CPU_TARGET_PERCENT", "25")) / 100,
            memory_target=float(os.environ.get("MEMORY_TARGET_PERCENT", "25")) / 100,
            sample_seconds=float(os.environ.get("SAMPLE_SECONDS", "2")),
            reserve_bytes=int(os.environ.get("MEMORY_RESERVE_MIB", "512")) * MIB,
            max_ballast_bytes=int(os.environ.get("MAX_BALLAST_MIB", "3584")) * MIB,
        )


@dataclasses.dataclass(frozen=True)
class CpuTimes:
    total: int
    busy: int


@dataclasses.dataclass(frozen=True)
class MemorySample:
    total: int
    available: int
    used: int


def parse_cpu_times(text: str) -> CpuTimes:
    line = next((line for line in text.splitlines() if line.startswith("cpu ")), "")
    try:
        values = [int(value) for value in line.split()[1:9]]
    except ValueError as exc:
        raise ValueError("Invalid aggregate CPU counters") from exc
    if len(values) < 4 or any(value < 0 for value in values):
        raise ValueError("Missing or invalid aggregate CPU counters")
    total = sum(values)
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return CpuTimes(total=total, busy=total - idle)


def cpu_utilization(previous: CpuTimes, current: CpuTimes) -> float | None:
    total = current.total - previous.total
    busy = current.busy - previous.busy
    if total <= 0 or busy < 0 or busy > total:
        return None
    return busy / total


def parse_memory(text: str) -> MemorySample:
    values = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                values[parts[0].rstrip(":")] = int(parts[1]) * 1024
            except ValueError as exc:
                raise ValueError("Invalid memory counters") from exc
    total = values.get("MemTotal", 0)
    free = values.get("MemFree", -1)
    if total <= 0 or free < 0 or free > total:
        raise ValueError("Missing or invalid physical memory counters")
    reclaimable = free + values.get("Buffers", 0) + values.get("Cached", 0) + values.get("SReclaimable", 0)
    available = values.get("MemAvailable", min(total, reclaimable))
    if not 0 <= available <= total:
        raise ValueError("Invalid available memory counter")
    used = max(0, total - min(total, reclaimable))
    return MemorySample(total=total, available=available, used=used)


class CpuController:
    def __init__(self, target: float, cpu_count: int, worker_count: int, max_duty: float = 0.40):
        if cpu_count <= 0 or worker_count <= 0:
            raise ValueError("CPU and worker counts must be positive")
        self.target = target
        self.scale = cpu_count / worker_count
        self.max_duty = max_duty
        self.duty = 0.0

    def update(self, measured: float | None) -> float:
        if measured is None or not math.isfinite(measured) or not 0 <= measured <= 1:
            return self.duty
        self.duty = min(self.max_duty, max(0.0, self.duty + 0.5 * (self.target - measured) * self.scale))
        return self.duty


def desired_ballast_bytes(current: int, sample: MemorySample, config: Config) -> int:
    target = int(sample.total * config.memory_target)
    desired = max(0, current + target - sample.used)
    cap = min(config.max_ballast_bytes, int(sample.total * config.max_ballast_fraction))
    desired = min(desired, cap, current + max(0, sample.available - config.reserve_bytes))
    if sample.available < config.reserve_bytes:
        desired = min(desired, max(0, current - (config.reserve_bytes - sample.available)))
    if desired > current:
        desired = min(desired, current + config.growth_bytes)
    return max(0, desired // config.chunk_bytes * config.chunk_bytes)


class MemoryBallast:
    def __init__(self, chunk_bytes: int = 8 * MIB):
        self.chunk_bytes = chunk_bytes
        self.chunks = []
        self.touch_index = 0

    @property
    def size(self):
        return len(self.chunks) * self.chunk_bytes

    @staticmethod
    def touch(mapping):
        for offset in range(0, len(mapping), mmap.PAGESIZE):
            mapping[offset] = 1

    def resize(self, requested: int):
        count = max(0, requested // self.chunk_bytes)
        while len(self.chunks) > count:
            self.chunks.pop().close()
        while len(self.chunks) < count:
            mapping = mmap.mmap(-1, self.chunk_bytes, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
            try:
                self.touch(mapping)
            except BaseException:
                mapping.close()
                raise
            self.chunks.append(mapping)

    def touch_one(self):
        if self.chunks:
            self.touch_index %= len(self.chunks)
            self.touch(self.chunks[self.touch_index])
            self.touch_index += 1

    def close(self):
        self.resize(0)


def write_status(path: Path, status: dict):
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(status, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def cpu_worker(duty, stop):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    value = 1
    period = 0.10
    while not stop.is_set():
        started = time.monotonic()
        with duty.get_lock():
            fraction = duty.value
        deadline = started + period * fraction
        while time.monotonic() < deadline and not stop.is_set():
            for _ in range(512):
                value = (value * 1664525 + 1013904223) & 0xFFFFFFFF
        stop.wait(max(0.0, period - (time.monotonic() - started)))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = Config.from_environment()
    cpu_count = os.cpu_count() or 1
    worker_count = int(os.environ.get("WORKER_COUNT", str(min(cpu_count, 8))))
    if not 1 <= worker_count <= cpu_count:
        raise ValueError("The worker count must be between one and the CPU count")
    max_runtime = float(os.environ.get("MAX_RUNTIME_SECONDS", "0"))
    status_path = Path(os.environ.get("STATUS_PATH", "/var/lib/resource-guard/status.json"))
    context = multiprocessing.get_context("spawn")
    stop = context.Event()
    duty = context.Value("d", 0.0)
    controller = CpuController(config.cpu_target, cpu_count, worker_count, config.max_cpu_duty)
    workers = [context.Process(target=cpu_worker, args=(duty, stop), daemon=True) for _ in range(worker_count)]
    ballast = MemoryBallast(config.chunk_bytes)
    started = time.monotonic()
    previous = parse_cpu_times(Path("/proc/stat").read_text())
    last_logged = 0.0

    def request_stop(signum, frame):
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    LOGGER.info("Starting resource controller: %s", dataclasses.asdict(config))
    try:
        for worker in workers:
            worker.start()
        while not stop.wait(config.sample_seconds):
            if any(not worker.is_alive() for worker in workers):
                raise RuntimeError("A CPU worker exited unexpectedly")
            current = parse_cpu_times(Path("/proc/stat").read_text())
            measured = cpu_utilization(previous, current)
            previous = current
            fraction = controller.update(measured)
            with duty.get_lock():
                duty.value = fraction
            memory = parse_memory(Path("/proc/meminfo").read_text())
            desired = desired_ballast_bytes(ballast.size, memory, config)
            try:
                ballast.resize(desired)
                ballast.touch_one()
            except (OSError, MemoryError):
                ballast.close()
                LOGGER.exception("Released ballast after an allocation failure")
            memory = parse_memory(Path("/proc/meminfo").read_text())
            uptime = time.monotonic() - started
            status = {
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "guest_cpu_percent": round(measured * 100, 2) if measured is not None else None,
                "guest_memory_percent": round(memory.used / memory.total * 100, 2),
                "cpu_target_percent": config.cpu_target * 100,
                "memory_target_percent": config.memory_target * 100,
                "worker_duty_percent": round(fraction * 100, 2),
                "workers": worker_count,
                "ballast_bytes": ballast.size,
                "memory_available_bytes": memory.available,
                "uptime_seconds": round(uptime, 1),
            }
            write_status(status_path, status)
            if uptime - last_logged >= 10:
                LOGGER.info("status=%s", json.dumps(status, sort_keys=True))
                last_logged = uptime
            if max_runtime > 0 and uptime >= max_runtime:
                break
    finally:
        stop.set()
        ballast.close()
        for worker in workers:
            if worker.pid is not None:
                worker.join(timeout=3)
                if worker.is_alive():
                    worker.kill()
                    worker.join(timeout=3)
        LOGGER.info("Resource controller stopped")


if __name__ == "__main__":
    main()
