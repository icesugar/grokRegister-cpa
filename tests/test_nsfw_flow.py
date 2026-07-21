import unittest
from unittest.mock import MagicMock, patch

import grok_register_ttk as app


class NsfwFlowTests(unittest.TestCase):
    def test_pre_cancel_skips_nsfw_network(self):
        with patch.object(
            app.requests,
            "Session",
            side_effect=AssertionError("cancelled NSFW must not start a session"),
        ):
            ok, message = app.enable_nsfw_for_token(
                "sso-token",
                cancel_callback=lambda: True,
            )
        self.assertFalse(ok)
        self.assertEqual(message, "用户已停止")

    def _session_context(self):
        session = MagicMock()
        session.headers = {}
        context = MagicMock()
        context.__enter__.return_value = session
        context.__exit__.return_value = False
        return context

    def test_http_success_skips_browser_and_clearance_scan(self):
        with patch.object(app.requests, "Session", return_value=self._session_context()), patch.object(
            app, "set_tos_accepted", return_value=(True, "ok")
        ), patch.object(app, "set_birth_date", return_value=(True, "ok")), patch.object(
            app, "update_nsfw_settings", return_value=(True, "ok")
        ), patch.object(app, "enable_nsfw_via_browser") as browser_fallback, patch.object(
            app, "extract_cf_clearance_and_ua"
        ) as clearance_scan:
            ok, message = app.enable_nsfw_for_token("sso-token")

        self.assertTrue(ok)
        self.assertIn("HTTP 快速路径", message)
        browser_fallback.assert_not_called()
        clearance_scan.assert_not_called()

    def test_cloudflare_failure_falls_back_to_browser(self):
        with patch.object(app.requests, "Session", return_value=self._session_context()), patch.object(
            app, "set_tos_accepted", return_value=(True, "ok")
        ), patch.object(
            app,
            "set_birth_date",
            return_value=(False, "set_birth_date 被 Cloudflare 拦截，HTTP 403"),
        ), patch.object(app, "_active_page", return_value=object()), patch.object(
            app,
            "enable_nsfw_via_browser",
            return_value=(True, "成功开启 NSFW（浏览器内）"),
        ) as browser_fallback, patch.object(app, "extract_cf_clearance_and_ua") as clearance_scan:
            ok, message = app.enable_nsfw_for_token("sso-token")

        self.assertTrue(ok)
        self.assertIn("浏览器内", message)
        browser_fallback.assert_called_once_with(
            token="sso-token",
            log_callback=None,
            cancel_callback=None,
        )
        clearance_scan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
