# -*- coding: utf-8 -*-
"""代理池加载与轮换逻辑单测。"""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# 保证可 import 项目根
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import grok_register_ttk as app  # noqa: E402


class TestProxyPool(unittest.TestCase):
    def tearDown(self):
        app.set_current_proxy(None)

    def test_normalize_proxy_url(self):
        self.assertEqual(app.normalize_proxy_url(""), "")
        self.assertEqual(app.normalize_proxy_url("  # comment"), "")
        self.assertEqual(app.normalize_proxy_url("1.2.3.4:8080"), "http://1.2.3.4:8080")
        self.assertEqual(
            app.normalize_proxy_url("socks5://user:pass@5.6.7.8:1080"),
            "socks5://user:pass@5.6.7.8:1080",
        )
        self.assertEqual(
            app.normalize_proxy_url("http://127.0.0.1:7890"),
            "http://127.0.0.1:7890",
        )

    def test_load_proxy_pool(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "proxies.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(
                    "# comment\n"
                    "http://1.2.3.4:8080\n"
                    "socks5://user:pass@5.6.7.8:1080\n"
                    "9.9.9.9:3128\n"
                    "\n"
                )
            pool = app.load_proxy_pool(path)
            self.assertEqual(
                pool,
                [
                    "http://1.2.3.4:8080",
                    "socks5://user:pass@5.6.7.8:1080",
                    "http://9.9.9.9:3128",
                ],
            )

    def test_load_proxy_pool_missing(self):
        with self.assertRaises(FileNotFoundError):
            app.load_proxy_pool("/no/such/proxies.txt")

    def test_apply_proxy_for_index_pool(self):
        pool = ["http://a:1", "http://b:2"]
        logs = []
        ok = app.apply_proxy_for_index(0, pool, log_callback=logs.append)
        self.assertTrue(ok)
        self.assertEqual(app.get_active_proxy(), "http://a:1")
        self.assertEqual(app.get_proxies(), {"http": "http://a:1", "https": "http://a:1"})

        ok = app.apply_proxy_for_index(1, pool, log_callback=logs.append)
        self.assertTrue(ok)
        self.assertEqual(app.get_active_proxy(), "http://b:2")

        ok = app.apply_proxy_for_index(2, pool, log_callback=logs.append)
        self.assertFalse(ok)
        self.assertTrue(any("用尽" in x for x in logs))

    def test_apply_proxy_fallback_to_config(self):
        old = app.config.get("proxy")
        try:
            app.config["proxy"] = "http://cfg:7890"
            logs = []
            ok = app.apply_proxy_for_index(0, [], log_callback=logs.append)
            self.assertTrue(ok)
            self.assertEqual(app.get_active_proxy(), "http://cfg:7890")
        finally:
            app.config["proxy"] = old

    def test_test_proxy_connectivity_ok(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch.object(app.requests, "get", return_value=mock_resp) as mock_get:
            ok, msg = app.test_proxy_connectivity("http://p:1")
            self.assertTrue(ok)
            self.assertIn("200", msg)
            mock_get.assert_called()
            kwargs = mock_get.call_args.kwargs
            self.assertEqual(kwargs["proxies"], {"http": "http://p:1", "https": "http://p:1"})

    def test_test_proxy_connectivity_fail(self):
        with patch.object(app.requests, "get", side_effect=Exception("boom")):
            ok, msg = app.test_proxy_connectivity("http://bad:1")
            self.assertFalse(ok)
            self.assertIn("boom", msg)

    def test_acquire_skips_dead_proxy(self):
        pool = ["http://dead:1", "http://live:2"]
        logs = []

        def fake_test(proxy, timeout=10, log_callback=None):
            if "live" in proxy:
                return True, "ok"
            return False, "dead"

        with patch.object(app, "test_proxy_connectivity", side_effect=fake_test):
            ok, next_cursor = app.acquire_proxy_for_slot(
                pool, cursor=0, log_callback=logs.append, do_test=True
            )
        self.assertTrue(ok)
        self.assertEqual(next_cursor, 2)
        self.assertEqual(app.get_active_proxy(), "http://live:2")
        self.assertTrue(any("跳过" in x for x in logs))

    def test_acquire_exhausted(self):
        pool = ["http://dead:1"]
        with patch.object(app, "test_proxy_connectivity", return_value=(False, "nope")):
            ok, next_cursor = app.acquire_proxy_for_slot(pool, cursor=0, do_test=True)
        self.assertFalse(ok)
        self.assertEqual(next_cursor, 1)


if __name__ == "__main__":
    unittest.main()
