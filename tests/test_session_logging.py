import datetime
import os
import tempfile
import threading
import unittest

import grok_register_ttk as app


class SessionLoggingTests(unittest.TestCase):
    def setUp(self):
        self.original_log_path = app._session_log_path
        app._session_log_path = None

    def tearDown(self):
        app._session_log_path = self.original_log_path

    def test_creates_one_utf8_log_file_per_startup(self):
        with tempfile.TemporaryDirectory() as log_dir:
            now = datetime.datetime(2026, 7, 15, 8, 36, 55)
            path = app.initialize_session_log(log_dir=log_dir, now=now)

            self.assertEqual(path, os.path.join(log_dir, "app_20260715_083655.log"))
            self.assertTrue(os.path.isfile(path))
            self.assertEqual(app.initialize_session_log(log_dir=log_dir, now=now), path)

    def test_uses_suffix_if_same_second_filename_exists(self):
        with tempfile.TemporaryDirectory() as log_dir:
            existing = os.path.join(log_dir, "app_20260715_083655.log")
            with open(existing, "w", encoding="utf-8"):
                pass

            path = app.initialize_session_log(
                log_dir=log_dir,
                now=datetime.datetime(2026, 7, 15, 8, 36, 55),
            )

            self.assertEqual(path, os.path.join(log_dir, "app_20260715_083655_2.log"))

    def test_concurrent_writes_keep_complete_lines(self):
        with tempfile.TemporaryDirectory() as log_dir:
            path = app.initialize_session_log(log_dir=log_dir)
            expected = {f"worker-{worker}-line-{line}" for worker in range(4) for line in range(25)}

            def write_lines(worker):
                for line in range(25):
                    app.append_session_log(f"worker-{worker}-line-{line}")

            threads = [threading.Thread(target=write_lines, args=(worker,)) for worker in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            with open(path, encoding="utf-8") as log_file:
                self.assertEqual(set(log_file.read().splitlines()), expected)


if __name__ == "__main__":
    unittest.main()
