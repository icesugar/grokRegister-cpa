import unittest
from unittest.mock import MagicMock

import connectivity


class DummyResp:
    def __init__(self, status_code=200, text="ok"):
        self.status_code = status_code
        self.text = text


class ConnectivityTests(unittest.TestCase):
    def test_proxy_empty_is_ok(self):
        name, ok, detail = connectivity.check_proxy("", lambda *a, **k: DummyResp())
        self.assertTrue(ok)
        self.assertIn("未配置", detail)

    def test_cpa_disabled_skips(self):
        name, ok, detail = connectivity.check_cpa({"cpa_auto_add": False}, lambda *a, **k: DummyResp())
        self.assertTrue(ok)
        self.assertIn("未开启", detail)

    def test_cpa_enabled_needs_target(self):
        name, ok, detail = connectivity.check_cpa(
            {"cpa_auto_add": True, "cpa_auth_dir": "", "cpa_remote_url": ""},
            lambda *a, **k: DummyResp(),
        )
        self.assertFalse(ok)

    def test_email_cloudflare_missing_base(self):
        name, ok, detail = connectivity.check_email_api(
            "cloudflare",
            {},
            lambda *a, **k: DummyResp(),
            lambda *a, **k: DummyResp(),
        )
        self.assertFalse(ok)

    def test_email_cloudflare_ok(self):
        name, ok, detail = connectivity.check_email_api(
            "cloudflare",
            {"cloudflare_api_base": "https://mail.example.com"},
            lambda *a, **k: DummyResp(200),
            lambda *a, **k: DummyResp(),
        )
        self.assertTrue(ok)
        self.assertIn("200", detail)

    def test_email_cloudflare_unauthorized_is_failure(self):
        name, ok, detail = connectivity.check_email_api(
            "cloudflare",
            {
                "cloudflare_api_base": "https://mail.example.com",
                "cloudflare_api_key": "bad-secret",
                "cloudflare_auth_mode": "x-api-key",
                "cloudflare_path_accounts": "/admin/new_address",
            },
            lambda *a, **k: DummyResp(401),
            lambda *a, **k: DummyResp(),
        )
        self.assertFalse(ok)
        self.assertIn("401", detail)

    def test_email_cloudflare_direct_create_with_custom_auth_does_not_need_domains(self):
        name, ok, detail = connectivity.check_email_api(
            "cloudflare",
            {
                "cloudflare_api_base": "https://mail.example.com",
                "cloudflare_auth_mode": "none",
                "cloudflare_custom_auth": "global-secret",
                "cloudflare_path_accounts": "/api/new_address",
            },
            lambda *a, **k: DummyResp(401),
            lambda *a, **k: DummyResp(),
        )
        self.assertTrue(ok)
        self.assertIn("直建模式", detail)

    def test_email_cloudflare_uses_configured_auth(self):
        captured = {}

        def fake_get(url, **kwargs):
            captured.update(kwargs)
            return DummyResp(200)

        _, ok, _ = connectivity.check_email_api(
            "cloudflare",
            {
                "cloudflare_api_base": "https://mail.example.com",
                "cloudflare_api_key": "secret",
                "cloudflare_auth_mode": "x-api-key",
                "cloudflare_custom_auth": "global-secret",
            },
            fake_get,
            lambda *a, **k: DummyResp(),
        )
        self.assertTrue(ok)
        self.assertEqual(captured["headers"]["X-API-Key"], "secret")
        self.assertEqual(captured["headers"]["x-custom-auth"], "global-secret")

    def test_format_results(self):
        text = connectivity.format_check_results([("代理", True, "ok"), ("CPA", False, "bad")])
        self.assertIn("[OK] 代理", text)
        self.assertIn("[FAIL] CPA", text)


if __name__ == "__main__":
    unittest.main()
