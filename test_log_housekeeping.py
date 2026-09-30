#!/usr/bin/env python3
"""Runs-log rotation and the dashboard's tail read."""
from __future__ import annotations

import json
import random
import shutil
import tempfile
import unittest
from pathlib import Path

import coinbase_spot_bot as bot
import trade_analytics_dashboard as dash


class LogHousekeepingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cb-logs-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / "logs" / "runs.jsonl"

    def test_rotation_caps_total_files_and_keeps_newest_entries(self) -> None:
        for i in range(40):
            bot.append_jsonl_rotating(self.path, {"i": i, "pad": "x" * 50}, max_bytes=200, backups=3)
        names = sorted(p.name for p in self.path.parent.iterdir())
        self.assertEqual(names, ["runs.jsonl", "runs.jsonl.1", "runs.jsonl.2", "runs.jsonl.3"])
        newest = [json.loads(l)["i"] for l in self.path.read_text().splitlines()]
        self.assertEqual(newest[-1], 39)
        older = [json.loads(l)["i"] for l in (self.path.parent / "runs.jsonl.1").read_text().splitlines()]
        self.assertLess(max(older), min(newest))

    def test_zero_max_bytes_disables_rotation(self) -> None:
        for i in range(20):
            bot.append_jsonl_rotating(self.path, {"i": i}, max_bytes=0)
        self.assertEqual(len(self.path.read_text().splitlines()), 20)
        self.assertEqual(len(list(self.path.parent.iterdir())), 1)

    def test_run_log_uses_config_limits(self) -> None:
        cfg = {"runs_log_path": str(self.path), "runs_log_max_bytes": 1, "runs_log_backups": 1}
        bot.append_run_log(cfg, {"n": 1})
        bot.append_run_log(cfg, {"n": 2})
        self.assertEqual(json.loads(self.path.read_text())["n"], 2)
        self.assertEqual(json.loads((self.tmp / "logs" / "runs.jsonl.1").read_text())["n"], 1)

    def test_tail_read_matches_full_parse_across_block_boundaries(self) -> None:
        rnd = random.Random(7)
        self.path.parent.mkdir(parents=True)
        for trial in range(50):
            rows = [{"i": i, "pad": "y" * rnd.randint(0, 300)} for i in range(rnd.randint(1, 30))]
            self.path.write_text("".join(json.dumps(r) + "\n" for r in rows))
            for block in (8, 64, 65536):
                self.assertEqual(dash.latest_jsonl(self.path, block_size=block), rows[-1])

    def test_tail_read_skips_a_truncated_last_line(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"ok": 1}) + "\n" + '{"half": ')
        self.assertEqual(dash.latest_jsonl(self.path, block_size=4), {"ok": 1})

    def test_tail_read_handles_missing_and_empty_files(self) -> None:
        self.assertEqual(dash.latest_jsonl(self.tmp / "missing.jsonl"), {})
        self.path.parent.mkdir(parents=True)
        self.path.write_text("")
        self.assertEqual(dash.latest_jsonl(self.path), {})


if __name__ == "__main__":
    unittest.main()
