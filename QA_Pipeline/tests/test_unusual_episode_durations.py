import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path

from Werkzeuge.check_unusual_episode_durations import (
    ALGORITHM_VERSION,
    DurationAnomalyStore,
    classify,
    export_csv,
    robust_profile,
    write_html_report,
)


class DurationAlgorithmTests(unittest.TestCase):
    def test_robust_profile_rejects_long_and_short_extremes(self):
        profile = robust_profile([58, 59, 60, 61, 62] * 8 + [4, 300])
        self.assertGreater(profile["lower"], 4)
        self.assertLess(profile["upper"], 300)
        self.assertEqual(classify(4, profile)["direction"], "short")
        self.assertEqual(classify(300, profile)["direction"], "long")

    def test_flat_distribution_keeps_a_minimum_margin(self):
        profile = robust_profile([60] * 40)
        self.assertLessEqual(profile["lower"], 54)
        self.assertGreaterEqual(profile["upper"], 66)


class StandaloneDurationScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "verified"
        self.store = DurationAnomalyStore(Path(self.temp.name) / "result.sqlite")

    def tearDown(self):
        self.temp.cleanup()

    def episode(self, name: str, duration: float, label: str = "完全正常", operator: str = "alice") -> Path:
        path = self.root / "wipe_table" / "20260715" / operator / name
        path.mkdir(parents=True, exist_ok=True)
        (path / "metadata.json").write_text(
            json.dumps({
                "task_key": "wipe_table",
                "duration_seconds": duration,
                "quality": {"labels": [label]},
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def fixture(self) -> Path:
        for index in range(30):
            self.episode(f"episode_{index:04d}", 58 + index % 5)
        long_episode = self.episode("episode_0100", 300)
        self.episode("episode_0101", 4, operator="bob")
        self.episode("episode_0102", 500, label="测试任务")
        return long_episode

    def test_scan_is_read_only_exact_normal_and_long_first(self):
        long_episode = self.fixture()
        metadata = long_episode / "metadata.json"
        before = (hashlib.sha256(metadata.read_bytes()).hexdigest(), metadata.stat().st_mtime_ns)

        status = self.store.scan(self.root, full=True)

        after = (hashlib.sha256(metadata.read_bytes()).hexdigest(), metadata.stat().st_mtime_ns)
        self.assertEqual(before, after)
        self.assertEqual(status["algorithm_version"], ALGORITHM_VERSION)
        self.assertEqual(status["eligible"], 32)
        rows = self.store.anomaly_rows()
        self.assertEqual([row["direction"] for row in rows], ["long", "short"])
        self.assertNotIn("episode_0102", {row["episode_name"] for row in rows})

        groups = self.store.group_rows()
        alice = next(row for row in groups if row["operator"] == "alice")
        self.assertEqual(alice["long_count"], 1)
        self.assertEqual(alice["eligible"], 31)

        csv_path = Path(self.temp.name) / "long.csv"
        html_path = Path(self.temp.name) / "report.html"
        self.assertEqual(export_csv(self.store, csv_path, direction="long"), 1)
        self.assertEqual(write_html_report(self.store, html_path), 2)
        self.assertIn("episode_0100", csv_path.read_text(encoding="utf-8"))
        self.assertIn("Episode 时长异常复查", html_path.read_text(encoding="utf-8"))

    def test_incremental_scan_reuses_unchanged_and_reloads_changed_metadata(self):
        self.fixture()
        self.store.scan(self.root, full=True)
        second = self.store.scan(self.root)
        self.assertEqual(second["last_run"]["reused"], 33)

        target = self.root / "wipe_table" / "20260715" / "alice" / "episode_0000" / "metadata.json"
        time.sleep(0.002)
        target.write_text(json.dumps({
            "task_key": "wipe_table",
            "duration_seconds": 301,
            "quality": {"labels": ["完全正常"]},
        }, ensure_ascii=False), encoding="utf-8")
        third = self.store.scan(self.root)
        self.assertEqual(third["last_run"]["parsed"], 1)
        self.assertIn("episode_0000", {row["episode_name"] for row in self.store.anomaly_rows()})

    def test_malformed_changed_task_preserves_previous_complete_results(self):
        self.fixture()
        self.store.scan(self.root, full=True)
        before = [dict(row) for row in self.store.anomaly_rows()]
        broken = self.root / "wipe_table" / "20260715" / "alice" / "episode_0001" / "metadata.json"
        broken.write_text("{broken", encoding="utf-8")

        status = self.store.scan(self.root)

        self.assertEqual(status["last_run"]["status"], "partial")
        self.assertEqual(status["last_run"]["incomplete_tasks"], ["wipe_table"])
        self.assertEqual(before, [dict(row) for row in self.store.anomaly_rows()])


if __name__ == "__main__":
    unittest.main()
