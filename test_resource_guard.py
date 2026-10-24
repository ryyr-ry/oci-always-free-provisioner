import math
import tempfile
import unittest
from pathlib import Path

from resource_guard import (
    Config,
    CpuController,
    CpuTimes,
    MemoryBallast,
    MemorySample,
    cpu_utilization,
    desired_ballast_bytes,
    parse_cpu_times,
    parse_memory,
    write_status,
)

MIB = 1024 * 1024
GIB = 1024 * MIB


class CpuMeasurementTests(unittest.TestCase):
    def test_guest_counters_are_not_counted_twice(self):
        sample = parse_cpu_times("cpu 100 10 40 800 20 5 5 20 30 2\ncpu0 1 2 3 4\n")
        self.assertEqual(sample, CpuTimes(total=1000, busy=180))

    def test_iowait_does_not_count_as_cpu_work(self):
        before = CpuTimes(total=100, busy=20)
        after = parse_cpu_times("cpu 20 0 0 80 100 0 0 0\n")
        self.assertEqual(cpu_utilization(before, after), 0.0)

    def test_utilization_uses_counter_differences(self):
        self.assertAlmostEqual(cpu_utilization(CpuTimes(100, 30), CpuTimes(300, 80)), 0.25)

    def test_invalid_or_reset_counters_produce_no_measurement(self):
        for current in [CpuTimes(100, 40), CpuTimes(90, 40), CpuTimes(200, 20)]:
            with self.subTest(current=current):
                self.assertIsNone(cpu_utilization(CpuTimes(100, 30), current))

    def test_malformed_cpu_input_is_rejected(self):
        for text in ["", "cpu0 1 2 3 4", "cpu 1 2", "cpu -1 0 0 10"]:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_cpu_times(text)


class CpuControlTests(unittest.TestCase):
    def test_control_converges_with_existing_workload(self):
        controller = CpuController(target=0.25, cpu_count=2, worker_count=2)
        duty = 0.0
        for _ in range(40):
            duty = controller.update(min(1.0, 0.10 + duty))
        self.assertAlmostEqual(0.10 + duty, 0.25, delta=0.01)

    def test_control_yields_when_other_work_exceeds_target(self):
        controller = CpuController(target=0.25, cpu_count=2, worker_count=2)
        for _ in range(20):
            controller.update(0.0)
        for _ in range(20):
            duty = controller.update(0.60)
        self.assertEqual(duty, 0.0)

    def test_cpu_duty_is_capped(self):
        controller = CpuController(target=0.25, cpu_count=2, worker_count=2, max_duty=0.40)
        for _ in range(100):
            duty = controller.update(0.0)
        self.assertEqual(duty, 0.40)

    def test_fewer_workers_are_scaled_against_total_cpus(self):
        controller = CpuController(target=0.25, cpu_count=2, worker_count=1, max_duty=0.80)
        duty = 0.0
        for _ in range(40):
            duty = controller.update(duty / 2)
        self.assertAlmostEqual(duty, 0.50, delta=0.02)

    def test_invalid_measurements_do_not_change_duty(self):
        controller = CpuController(target=0.25, cpu_count=2, worker_count=2)
        before = controller.update(0.10)
        for value in [None, math.nan, -0.1, 1.1]:
            self.assertEqual(controller.update(value), before)


class MemoryMeasurementTests(unittest.TestCase):
    def test_reclaimable_cache_is_excluded_conservatively(self):
        sample = parse_memory(
            "MemTotal: 1000 kB\nMemFree: 400 kB\nMemAvailable: 700 kB\n"
            "Buffers: 50 kB\nCached: 150 kB\nSReclaimable: 100 kB\n"
        )
        self.assertEqual(sample, MemorySample(total=1000 * 1024, available=700 * 1024, used=300 * 1024))

    def test_missing_available_uses_reclaimable_memory(self):
        sample = parse_memory("MemTotal: 1000 kB\nMemFree: 400 kB\nBuffers: 50 kB\nCached: 150 kB\n")
        self.assertEqual(sample.available, 600 * 1024)
        self.assertEqual(sample.used, 400 * 1024)

    def test_invalid_memory_totals_are_rejected(self):
        for text in ["", "MemTotal: 0 kB\nMemFree: 0 kB", "MemTotal: 10 kB\nMemFree: 20 kB"]:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_memory(text)


class MemoryControlTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(memory_target=0.25, growth_bytes=128 * MIB, reserve_bytes=512 * MIB)

    def test_growth_is_bounded_per_iteration(self):
        sample = MemorySample(total=12 * GIB, available=11 * GIB, used=GIB)
        self.assertEqual(desired_ballast_bytes(0, sample, self.config), 128 * MIB)

    def test_existing_application_memory_is_not_duplicated(self):
        sample = MemorySample(total=12 * GIB, available=8 * GIB, used=4 * GIB)
        self.assertEqual(desired_ballast_bytes(GIB, sample, self.config), 0)

    def test_pressure_releases_memory_immediately(self):
        sample = MemorySample(total=12 * GIB, available=100 * MIB, used=10 * GIB)
        self.assertEqual(desired_ballast_bytes(2 * GIB, sample, self.config), 0)

    def test_growth_preserves_memory_headroom(self):
        sample = MemorySample(total=12 * GIB, available=520 * MIB, used=GIB)
        self.assertLessEqual(desired_ballast_bytes(0, sample, self.config), 8 * MIB)

    def test_an_absolute_memory_cap_is_enforced(self):
        config = Config(memory_target=0.25, max_ballast_bytes=16 * MIB)
        sample = MemorySample(total=12 * GIB, available=11 * GIB, used=GIB)
        self.assertLessEqual(desired_ballast_bytes(0, sample, config), 16 * MIB)

    def test_target_reached_does_not_add_memory(self):
        sample = MemorySample(total=12 * GIB, available=9 * GIB, used=3 * GIB)
        self.assertEqual(desired_ballast_bytes(2 * GIB, sample, self.config), 2 * GIB)


class BallastAndStatusTests(unittest.TestCase):
    def test_ballast_commits_and_releases_pages(self):
        def rss():
            for line in Path("/proc/self/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
            self.fail("VmRSS was not found")

        before = rss()
        ballast = MemoryBallast(chunk_bytes=4 * MIB)
        ballast.resize(8 * MIB)
        self.assertEqual(ballast.size, 8 * MIB)
        self.assertGreaterEqual(rss() - before, 6 * MIB)
        ballast.touch_one()
        ballast.resize(0)
        self.assertEqual(ballast.size, 0)
        self.assertLess(rss() - before, 4 * MIB)

    def test_partial_chunks_never_exceed_the_requested_size(self):
        ballast = MemoryBallast(chunk_bytes=4 * MIB)
        try:
            ballast.resize(6 * MIB)
            self.assertEqual(ballast.size, 4 * MIB)
        finally:
            ballast.close()

    def test_status_is_valid_json_and_replaced_atomically(self):
        import json

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            write_status(path, {"cpu_percent": 25.0})
            write_status(path, {"cpu_percent": 26.0})
            self.assertEqual(json.loads(path.read_text()), {"cpu_percent": 26.0})
            self.assertEqual(list(Path(directory).iterdir()), [path])


class ConfigurationTests(unittest.TestCase):
    def test_defaults_have_margin_above_idle_thresholds(self):
        config = Config()
        self.assertGreater(config.cpu_target, 0.20)
        self.assertGreater(config.memory_target, 0.20)

    def test_invalid_configuration_is_rejected(self):
        for kwargs in [
            {"cpu_target": -0.1},
            {"cpu_target": math.nan},
            {"memory_target": 1.1},
            {"sample_seconds": 0},
            {"reserve_bytes": -1},
            {"max_ballast_bytes": -1},
        ]:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    Config(**kwargs)


if __name__ == "__main__":
    unittest.main()
