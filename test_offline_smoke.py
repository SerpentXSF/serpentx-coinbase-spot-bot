#!/usr/bin/env python3
"""Offline smoke tests for the documented quick-start commands.

Coinbase and every optional context provider are replaced with a fake HTTP
layer, so these run without network access or API keys. They exercise the same
entry points a new user runs from the README (status run, preview run, rotator,
exit monitor, dashboard summary) against a throwaway copy of the repo.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent
SCRIPTS = [
    "coinbase_spot_bot.py",
    "coinbase_client.py",
    "analyze_usdc_pairs.py",
    "rotate_and_run.py",
    "exit_monitor.py",
    "trade_analytics_dashboard.py",
    "candidate_forward_backtest.py",
    "reconcile_orders.py",
]

# Injected into subprocesses via sitecustomize so child scripts also use the fake API.
FAKE_HTTP = r'''
import json, time, requests

PRODUCTS = ["AAA-USDC", "BBB-USDC", "CCC-USDC", "DDD-USDC", "EEE-USDC", "FFF-USDC"]

class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)
        self.headers = {}
    def json(self):
        return self._payload
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

def product(pid):
    return {"product_id": pid, "price": "1.10", "price_percentage_change_24h": "3.5",
            "volume_24h": "20000000", "status": "online", "quote_min_size": "1",
            "base_increment": "0.01", "quote_increment": "0.0001", "base_currency_id": pid.split("-")[0],
            "quote_currency_id": "USDC"}

def candles(params):
    gran = {"FIVE_MINUTE": 300, "FIFTEEN_MINUTE": 900, "THIRTY_MINUTE": 1800,
            "ONE_HOUR": 3600, "ONE_DAY": 86400}.get(params.get("granularity"), 900)
    end = int(params.get("end") or time.time())
    start = int(params.get("start") or end - 200 * gran)
    n = max(1, min(300, (end - start) // gran))
    out = []
    for i in range(n):
        close = 1.0 + i * 0.001 + (0.004 if i % 3 == 0 else 0.0)
        out.append({"start": str(start + i * gran), "open": close, "high": close * 1.01,
                    "low": close * 0.99, "close": close, "volume": "1000"})
    return {"candles": out}

def fake_get(url, params=None, **kw):
    params = params or {}
    if "api.coinbase.com" not in url:
        raise requests.ConnectionError("offline test: external provider disabled")
    path = url.split("api.coinbase.com", 1)[1]
    if path == "/api/v3/brokerage/market/products":
        return FakeResponse(200, {"products": [product(p) for p in PRODUCTS]})
    if path.endswith("/candles"):
        return FakeResponse(200, candles(params))
    if "GONE-USDC" in path:
        return FakeResponse(404, {"error": "NOT_FOUND", "message": "product not found"})
    if path.startswith("/api/v3/brokerage/market/products/"):
        pid = path.rsplit("/", 1)[1]
        return FakeResponse(200, product(pid))
    return FakeResponse(404, {"error": "not found"})

def fake_request(method, url, **kw):
    if method.upper() == "GET":
        return fake_get(url, **kw)
    return FakeResponse(401, {"error": "unauthorized"})

requests.get = fake_get
requests.request = fake_request
# The bot reuses a requests.Session; route its calls through the same fake.
requests.Session.request = lambda self, method, url, **kw: fake_request(method, url, **kw)
'''


class OfflineSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cb-bot-smoke-"))
        self.root = self.tmp / "bot"
        self.root.mkdir()
        # Copy every runtime module (not just the entry points) so shared
        # modules such as indicators.py are importable, exactly like a clone.
        for src in REPO.glob("*.py"):
            if not src.name.startswith("test_"):
                shutil.copy(src, self.root / src.name)
        shutil.copy(REPO / "config.example.json", self.root / "config.json")
        # Mirror the README quick start exactly: copy the template unchanged.
        shutil.copy(REPO / ".env.example", self.root / ".env")
        cfg = json.loads((self.root / "config.json").read_text())
        cfg["allowed_products"] = ["AAA-USDC", "BBB-USDC"]
        (self.root / "config.json").write_text(json.dumps(cfg, indent=2))
        fake_dir = self.tmp / "fake"
        fake_dir.mkdir()
        (fake_dir / "sitecustomize.py").write_text(FAKE_HTTP)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("COINBASE_", "HELIUS_", "ALCHEMY_"))}
        self.env["PYTHONPATH"] = str(fake_dir)
        self.env.pop("COINBASE_BOT_ROOT", None)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_script(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [sys.executable, *args],
            cwd=str(cwd or self.root),
            env=self.env,
            text=True,
            capture_output=True,
            timeout=120,
        )
        return proc

    def assertOk(self, proc: subprocess.CompletedProcess) -> None:
        self.assertEqual(proc.returncode, 0, msg=f"stdout:\n{proc.stdout[-2000:]}\nstderr:\n{proc.stderr[-2000:]}")

    def test_all_scripts_show_help(self) -> None:
        for name in SCRIPTS:
            if name in {"coinbase_client.py"}:
                continue
            if name == "analyze_usdc_pairs.py":
                continue  # no CLI flags; exercised by the rotator test
            with self.subTest(script=name):
                self.assertOk(self.run_script(name, "--help"))

    def test_status_run_with_unedited_env_template(self) -> None:
        proc = self.run_script("coinbase_spot_bot.py", "--config", "config.json", "--status", "--json")
        self.assertOk(proc)
        result = json.loads(proc.stdout)
        self.assertEqual(result["decision"], "STATUS_ONLY")
        self.assertEqual(result.get("auth_status"), "missing_coinbase_credentials")
        self.assertTrue((self.root / "state" / "state.json").exists())
        self.assertTrue((self.root / "logs" / "runs.jsonl").exists())

    def test_preview_run_without_credentials_is_safe(self) -> None:
        proc = self.run_script("coinbase_spot_bot.py", "--config", "config.json", "--json")
        self.assertOk(proc)
        self.assertEqual(json.loads(proc.stdout)["decision"], "NEEDS_CREDENTIALS_FOR_PREVIEW_OR_TRADE")

    def test_one_delisted_product_does_not_abort_the_run(self) -> None:
        cfg = json.loads((self.root / "config.json").read_text())
        cfg["allowed_products"] = ["GONE-USDC", "AAA-USDC"]
        (self.root / "config.json").write_text(json.dumps(cfg))
        proc = self.run_script("coinbase_spot_bot.py", "--config", "config.json", "--status", "--json")
        self.assertOk(proc)
        result = json.loads(proc.stdout)
        self.assertEqual(result["top"]["product_id"], "AAA-USDC")
        gone = next(s for s in result["signals"] if s["product_id"] == "GONE-USDC")
        self.assertEqual(gone["action"], "ERROR")

    def test_runtime_files_land_in_repo_even_when_run_from_elsewhere(self) -> None:
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        proc = self.run_script(str(self.root / "coinbase_spot_bot.py"), "--config", str(self.root / "config.json"), "--status", cwd=elsewhere)
        self.assertOk(proc)
        self.assertTrue((self.root / "state" / "state.json").exists())
        self.assertFalse((elsewhere / "state").exists())

    def test_overlapping_runs_are_skipped_not_doubled(self) -> None:
        lock = self.root / "state" / "run.lock"
        lock.parent.mkdir()
        lock.write_text("{}")  # fresh lock held by "another" run
        proc = self.run_script("coinbase_spot_bot.py", "--json")
        self.assertOk(proc)
        self.assertEqual(json.loads(proc.stdout)["decision"], "SKIPPED_RUN_LOCKED")
        self.assertOk(self.run_script("exit_monitor.py"))
        self.assertTrue(lock.exists())

    def test_client_cli_uses_shared_auth(self) -> None:
        proc = self.run_script("coinbase_client.py", "accounts")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Missing required env var: COINBASE_API_KEY_NAME", proc.stderr)

    def test_rotator_preview(self) -> None:
        proc = self.run_script("rotate_and_run.py", "--json")
        self.assertOk(proc)
        summary = json.loads(proc.stdout)
        self.assertEqual(len(summary["rotated_to"]), 5)
        self.assertTrue((self.root / "analysis" / "usdc_pairs_latest.json").exists())
        cfg = json.loads((self.root / "config.json").read_text())
        self.assertEqual(cfg["allowed_products"], summary["rotated_to"])
        self.assertFalse(cfg["active_trading"])

    def test_exit_monitor_without_positions(self) -> None:
        proc = self.run_script("exit_monitor.py", "--json")
        self.assertOk(proc)
        self.assertEqual(json.loads(proc.stdout)["decision"], "NO_POSITIONS")

    def test_candidate_backtest_with_no_snapshots(self) -> None:
        proc = self.run_script("candidate_forward_backtest.py", "--snapshots", "2", "--top", "1")
        self.assertOk(proc)

    def test_dashboard_reads_bot_state_and_trade_log(self) -> None:
        (self.root / "state").mkdir()
        (self.root / "logs").mkdir()
        (self.root / "state" / "state.json").write_text(json.dumps({"product_cooldowns": {"AAA-USDC": "2999-01-01T00:00:00+00:00"}}))
        trade = {"ts": "2026-01-01T00:00:00+00:00", "side": "BUY", "quote_size": 15,
                 "order": {"success": True, "success_response": {"order_id": "abc", "product_id": "AAA-USDC", "side": "BUY"}}}
        (self.root / "logs" / "trades.jsonl").write_text(json.dumps(trade) + "\n")
        proc = self.run_script("trade_analytics_dashboard.py", "--json")
        self.assertOk(proc)
        summary = json.loads(proc.stdout)
        self.assertIn("AAA-USDC", json.dumps(summary))

    def test_dashboard_defaults_to_localhost(self) -> None:
        proc = self.run_script("trade_analytics_dashboard.py", "--help")
        self.assertOk(proc)
        self.assertIn("127.0.0.1", proc.stdout)


class PlaceholderCredentialTests(unittest.TestCase):
    def test_template_values_are_not_treated_as_real_credentials(self) -> None:
        import coinbase_spot_bot as bot

        env = {
            "COINBASE_API_KEY_NAME": "organizations/YOUR_ORG_ID/apiKeys/YOUR_KEY_ID",
            "COINBASE_API_PRIVATE_KEY": "PASTE_YOUR_COINBASE_PEM_PRIVATE_KEY_WITH_ESCAPED_NEWLINES",
        }
        with patch.dict(os.environ, env, clear=False):
            present = bot.env_present()
        self.assertFalse(present["COINBASE_API_KEY_NAME"])
        self.assertFalse(present["COINBASE_API_PRIVATE_KEY"])

    def test_real_looking_credentials_are_detected(self) -> None:
        import coinbase_spot_bot as bot

        env = {
            "COINBASE_API_KEY_NAME": "organizations/1234/apiKeys/abcd",
            "COINBASE_API_PRIVATE_KEY": "-----BEGIN EC PRIVATE KEY-----\\nMHc...\\n-----END EC PRIVATE KEY-----\\n",
        }
        with patch.dict(os.environ, env, clear=False):
            present = bot.env_present()
        self.assertTrue(present["COINBASE_API_KEY_NAME"])
        self.assertTrue(present["COINBASE_API_PRIVATE_KEY"])


if __name__ == "__main__":
    unittest.main()
