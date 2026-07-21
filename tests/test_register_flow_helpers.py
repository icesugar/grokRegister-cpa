import unittest
from unittest.mock import MagicMock, patch

import register_flow as rf


class RegisterFlowHelperTests(unittest.TestCase):
    def setUp(self):
        class Cancelled(Exception):
            pass

        class DomainRejected(Exception):
            def __init__(self, email="", message=""):
                self.email = email
                self.message = message
                super().__init__(message or "rejected")

        class RetryNeeded(Exception):
            pass

        rf.configure(
            RegistrationCancelled=Cancelled,
            EmailDomainRejected=DomainRejected,
            AccountRetryNeeded=RetryNeeded,
            raise_if_cancelled=lambda cb: (_ for _ in ()).throw(Cancelled("stop")) if cb and cb() else None,
            sleep_with_cancel=lambda s, cb=None: None,
            get_email_and_token=lambda: ("a@b.com", "tok"),
            get_oai_code=lambda *a, **k: "ABC-DEF",
        )

    def test_build_profile_shape(self):
        given, family, password = rf.build_profile()
        self.assertTrue(given)
        self.assertTrue(family)
        self.assertGreaterEqual(len(password), 8)

    def test_raise_if_cancelled(self):
        with self.assertRaises(Exception):
            rf.raise_if_cancelled(lambda: True)

    def test_detect_domain_rejection_empty_without_page(self):
        with patch.object(rf, "page", MagicMock(__bool__=lambda self: False)):
            # page proxy may still be truthy; force active path via run_js fail
            mock_page = MagicMock()
            mock_page.run_js.side_effect = RuntimeError("no page")
            with patch.object(rf, "page", mock_page):
                msg = rf.detect_email_domain_rejection("a@b.com")
                self.assertEqual(msg, "")

    def test_detect_domain_rejection_hit(self):
        mock_page = MagicMock()
        mock_page.run_js.return_value = "您的邮箱域名 web-library.net 已被拒绝。请使用其他邮箱"
        with patch.object(rf, "page", mock_page):
            msg = rf.detect_email_domain_rejection("u@web-library.net")
            self.assertIn("拒绝", msg)

    def test_wait_for_sso_reads_sso_rw_fallback(self):
        mock_page = MagicMock()
        mock_page.url = "https://accounts.x.ai/sign-up?redirect=grok-com"
        mock_page.cookies.return_value = [
            {"name": "sso-rw", "value": "tok-from-rw"},
            {"name": "cf_clearance", "value": "x"},
        ]
        mock_page.run_js.return_value = False
        with patch.object(rf, "page", mock_page), patch.object(
            rf, "refresh_active_page", lambda: None
        ), patch.object(rf, "active_page", lambda: mock_page):
            val = rf.wait_for_sso_cookie(timeout=5, log_callback=None)
            self.assertEqual(val, "tok-from-rw")


if __name__ == "__main__":
    unittest.main()
