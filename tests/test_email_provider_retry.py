import unittest

from email_providers import duckmail, yyds


class DummyResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class EmailProviderRetryTests(unittest.TestCase):
    def test_duckmail_retries_transient_detail_failure(self):
        detail_calls = 0

        def fake_get(url, **kwargs):
            nonlocal detail_calls
            if url.endswith("/messages"):
                return DummyResponse([{"id": "m1", "to": [{"address": "a@example.com"}]}])
            detail_calls += 1
            if detail_calls == 1:
                raise RuntimeError("temporary detail failure")
            return DummyResponse({"text": "ABC-123", "subject": "ABC-123 xAI"})

        code = duckmail.wait_for_code(
            fake_get,
            "https://mail.example.com",
            "token",
            "a@example.com",
            timeout=1,
            poll_interval=0,
            extract_code=lambda text, subject: "ABC-123" if "ABC-123" in text + subject else None,
            raise_if_cancelled=lambda callback: None,
            sleep_with_cancel=lambda seconds, callback: None,
        )
        self.assertEqual(code, "ABC-123")
        self.assertEqual(detail_calls, 2)

    def test_yyds_retries_transient_detail_failure_with_empty_recipient_list(self):
        detail_calls = 0

        def fake_get(url, **kwargs):
            nonlocal detail_calls
            if url.endswith("/messages"):
                return DummyResponse({"success": True, "data": {"messages": [{"id": "m1"}]}})
            detail_calls += 1
            if detail_calls == 1:
                raise RuntimeError("temporary detail failure")
            return DummyResponse(
                {
                    "success": True,
                    "data": {"text": "ABC-123", "subject": "ABC-123 xAI", "html": []},
                }
            )

        code = yyds.wait_for_code(
            fake_get,
            "token",
            "a@example.com",
            timeout=1,
            poll_interval=0,
            raise_if_cancelled=lambda callback: None,
            sleep_with_cancel=lambda seconds, callback: None,
        )
        self.assertEqual(code, "ABC-123")
        self.assertEqual(detail_calls, 2)


if __name__ == "__main__":
    unittest.main()
