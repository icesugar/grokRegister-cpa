import unittest
from unittest.mock import patch

import cf_mail_debug
import grok_register_ttk as app


class DummyResponse:
    def __init__(self, payload):
        self._payload = payload
        self.text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class CloudflareAdminCreateTests(unittest.TestCase):
    def setUp(self):
        self.original_config = app.config.copy()
        self.original_cf_domain_index = app._cf_domain_index
        app._cf_domain_index = 0
        app.config = app.DEFAULT_CONFIG.copy()

    def tearDown(self):
        app.config = self.original_config
        app._cf_domain_index = self.original_cf_domain_index

    def test_default_config_keeps_cloudflare_temp_email_new_address(self):
        app.config = app.DEFAULT_CONFIG.copy()
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return DummyResponse({"address": "anon@example.com", "jwt": "default-jwt"})

        with patch.object(app, "http_post", side_effect=fake_post):
            address, jwt = app.cloudflare_create_temp_address("https://temp-mail.example.com")

        self.assertEqual(address, "anon@example.com")
        self.assertEqual(jwt, "default-jwt")
        self.assertEqual(captured["url"], "https://temp-mail.example.com/api/new_address")
        self.assertEqual(captured["json"], {})
        self.assertEqual(captured["headers"], {"Content-Type": "application/json"})

    def test_app_uses_admin_new_address_with_x_admin_auth(self):
        app.config.update({
            "cloudflare_backend": "temp_email",
            "cloudflare_api_key": "admin-secret",
            "cloudflare_auth_mode": "x-admin-auth",
            "cloudflare_path_accounts": "/admin/new_address",
            "defaultDomains": "vitassk.com",
        })
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return DummyResponse({"address": "adminuser@vitassk.com", "jwt": "address-jwt"})

        with patch.object(app, "generate_username", return_value="adminuser"), \
                patch.object(app, "http_post", side_effect=fake_post):
            address, jwt = app.cloudflare_create_temp_address("https://temp-mail.ikun.day")

        self.assertEqual(address, "adminuser@vitassk.com")
        self.assertEqual(jwt, "address-jwt")
        self.assertEqual(captured["url"], "https://temp-mail.ikun.day/admin/new_address")
        self.assertEqual(captured["json"], {
            "name": "adminuser",
            "domain": "vitassk.com",
            "enablePrefix": True,
        })
        self.assertEqual(captured["headers"]["Content-Type"], "application/json")
        self.assertEqual(captured["headers"]["x-admin-auth"], "admin-secret")

    def test_app_keeps_anonymous_new_address_with_none_auth(self):
        app.config.update({
            "cloudflare_backend": "temp_email",
            "cloudflare_api_key": "",
            "cloudflare_auth_mode": "none",
            "cloudflare_custom_auth": "",
            "cloudflare_path_accounts": "/api/new_address",
            "defaultDomains": "vitassk.com",
        })
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return DummyResponse({"address": "anon@vitassk.com", "jwt": "anon-jwt"})

        with patch.object(app, "http_post", side_effect=fake_post):
            address, jwt = app.cloudflare_create_temp_address("https://temp-mail.ikun.day")

        self.assertEqual(address, "anon@vitassk.com")
        self.assertEqual(jwt, "anon-jwt")
        self.assertEqual(captured["url"], "https://temp-mail.ikun.day/api/new_address")
        self.assertEqual(captured["json"], {"domain": "vitassk.com"})
        self.assertEqual(captured["headers"], {"Content-Type": "application/json"})

    def test_app_injects_custom_auth_on_anonymous_new_address(self):
        app.config.update({
            "cloudflare_backend": "temp_email",
            "cloudflare_api_key": "",
            "cloudflare_auth_mode": "none",
            "cloudflare_custom_auth": "global-pass",
            "cloudflare_path_accounts": "/api/new_address",
            "defaultDomains": "vitassk.com",
        })
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return DummyResponse({"address": "anon@vitassk.com", "jwt": "anon-jwt"})

        with patch.object(app, "http_post", side_effect=fake_post):
            app.cloudflare_create_temp_address("https://temp-mail.ikun.day")

        self.assertEqual(captured["headers"], {
            "Content-Type": "application/json",
            "x-custom-auth": "global-pass",
        })

    def test_freemail_generate_uses_admin_token_header(self):
        app.config.update({
            "cloudflare_backend": "freemail",
            "cloudflare_api_key": "fm-admin-token",
            "defaultDomains": "edu.example.com",
        })
        captured = {}

        class FakeSession:
            def __init__(self):
                self.headers = {"X-Admin-Token": "fm-admin-token"}

            def get(self, url, params=None, timeout=15):
                captured["url"] = url
                captured["params"] = params
                captured["headers"] = dict(self.headers)
                return DummyResponse({"email": "abc@edu.example.com"})

        class FakeClient:
            def __init__(self, api_base, token, get_proxies=None, timeout=20):
                self.api_base = api_base
                self.token = token
                self.get_proxies = get_proxies
                self.session = FakeSession()

            def create_temp_address(self, preferred_domain="", length=10):
                FakeClient._seen = {
                    "get_proxies": self.get_proxies,
                    "api_base": self.api_base,
                    "token": self.token,
                }
                resp = self.session.get(
                    f"{self.api_base}/api/generate",
                    params={"length": length},
                    timeout=15,
                )
                data = resp.json()
                return data["email"], ""

        with patch.object(app.freemail_provider, "FreemailClient", FakeClient):
            address, jwt = app.cloudflare_create_temp_address("https://freemail.example.com")

        self.assertEqual(address, "abc@edu.example.com")
        self.assertEqual(jwt, "")
        self.assertEqual(captured["url"], "https://freemail.example.com/api/generate")
        self.assertEqual(captured["headers"]["X-Admin-Token"], "fm-admin-token")
        # 主程序必须注入 get_proxies，注册时 freemail 才能走代理池
        self.assertIs(FakeClient._seen["get_proxies"], app.get_proxies)

    def test_debug_tool_can_create_address_through_admin_api(self):
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return DummyResponse({"address": "debuguser@vitassk.com", "jwt": "debug-jwt"})

        with patch.object(cf_mail_debug.requests, "post", side_effect=fake_post):
            address, jwt = cf_mail_debug.create_address(
                "https://temp-mail.ikun.day",
                auth_mode="x-admin-auth",
                api_key="admin-secret",
                create_path="/admin/new_address",
                domain="vitassk.com",
                name="debuguser",
            )

        self.assertEqual(address, "debuguser@vitassk.com")
        self.assertEqual(jwt, "debug-jwt")
        self.assertEqual(captured["url"], "https://temp-mail.ikun.day/admin/new_address")
        self.assertEqual(captured["json"], {
            "name": "debuguser",
            "domain": "vitassk.com",
            "enablePrefix": True,
        })
        self.assertEqual(captured["headers"]["Content-Type"], "application/json")
        self.assertEqual(captured["headers"]["x-admin-auth"], "admin-secret")


if __name__ == "__main__":
    unittest.main()
