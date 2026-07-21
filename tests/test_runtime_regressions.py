import os
import queue
import signal
import threading
import unittest
from unittest.mock import MagicMock, patch

import grok_register_ttk as app


class RuntimeRegressionTests(unittest.TestCase):
    def setUp(self):
        self.original_config = app.config.copy()

    def tearDown(self):
        app.config = self.original_config

    def test_empty_proxy_does_not_invent_local_cpa_proxy(self):
        app.config["proxy"] = ""
        empty_proxy_env = {
            "http_proxy": "",
            "HTTP_PROXY": "",
            "https_proxy": "",
            "HTTPS_PROXY": "",
        }
        with patch.dict(os.environ, empty_proxy_env, clear=False):
            self.assertEqual(app._resolve_cpa_proxy(), "")

    def test_gui_stop_first_click_sets_state_and_disables_button(self):
        gui = object.__new__(app.GrokRegisterGUI)
        gui.is_running = True
        gui.sso_convert_running = False
        gui.stop_requested = False
        gui.stop_btn = MagicMock()
        gui.status_var = MagicMock()
        gui.status_label = MagicMock()
        gui.close_browser_on_stop_var = MagicMock()
        gui.close_browser_on_stop_var.get.return_value = True
        logs = []
        gui.log = logs.append

        gui.stop_registration()
        gui.stop_registration()

        self.assertTrue(gui.stop_requested)
        gui.stop_btn.config.assert_called_once_with(state=app.tk.DISABLED)
        gui.status_var.set.assert_called_once_with("正在停止...")
        self.assertEqual(len(logs), 1)

    def test_cancelled_cpa_conversion_does_not_start_or_append_pending(self):
        app.config["cpa_auto_add"] = True
        app.config["cpa_auth_dir"] = "auths"
        with patch.object(
            app._s2cpa,
            "sso_to_token",
            side_effect=AssertionError("cancelled CPA must not exchange tokens"),
        ), patch.object(app, "_append_sso_pending") as append_pending:
            self.assertFalse(
                app.add_sso_to_cpa(
                    "sso-token",
                    email="user@example.com",
                    should_stop=lambda: True,
                )
            )
        append_pending.assert_not_called()

    def test_parallel_browser_start_failure_counts_all_tasks(self):
        app.config["register_workers"] = 2
        app.config["proxy_pool_file"] = ""
        app.config["proxy"] = ""
        import proxy_pool as _pp
        _pp.reset_allocator([])
        logs = []
        previous_handler = signal.getsignal(signal.SIGINT)
        try:
            with patch.object(app, "start_browser", side_effect=RuntimeError("boot failed")), patch.object(
                app, "cli_log", side_effect=logs.append
            ), patch.object(app, "maybe_stop_browser", return_value=None), patch.object(
                app, "_acquire_proxy_for_account", return_value=True
            ):
                app.run_registration_cli(2)
        finally:
            signal.signal(signal.SIGINT, previous_handler)

        self.assertTrue(any("成功 0 | 失败 2" in line for line in logs), logs)

    def test_gui_worker_log_is_drained_on_ui_thread(self):
        gui = object.__new__(app.GrokRegisterGUI)
        gui._ui_thread_id = threading.get_ident()
        gui.ui_queue = queue.Queue()
        gui.root = MagicMock()
        gui.log_text = MagicMock()

        worker = threading.Thread(target=gui.log, args=("worker message",))
        worker.start()
        worker.join()

        gui.log_text.insert.assert_not_called()
        gui._drain_ui_queue()
        gui.log_text.insert.assert_called_once()

    def test_account_write_failure_is_not_counted_as_success(self):
        gui = object.__new__(app.GrokRegisterGUI)
        gui.is_running = True
        gui.stop_requested = False
        gui.success_count = 0
        gui.fail_count = 0
        gui.fail_stats = app.empty_fail_stats()
        gui.results = []
        gui.accounts_output_file = "unwritable.txt"
        gui._stats_lock = threading.Lock()
        gui._accounts_lock = threading.Lock()
        gui.log = lambda message: None
        gui.update_stats = lambda: None

        with patch.object(app, "start_browser", return_value=(object(), object())), patch.object(
            app, "open_signup_page", return_value=None
        ), patch.object(app, "fill_email_and_submit", return_value=("a@example.com", "mail-token")), patch.object(
            app, "fill_code_and_submit", return_value="ABC-123"
        ), patch.object(
            app,
            "fill_profile_and_submit",
            return_value={"given_name": "A", "family_name": "B", "password": "secret"},
        ), patch.object(app, "wait_for_sso_cookie", return_value="sso-token"), patch.object(
            app, "maybe_stop_browser", return_value=None
        ), patch.object(app, "stop_browser", return_value=None), patch.object(
            app, "add_sso_to_cpa"
        ) as add_to_cpa, patch.object(app, "_append_sso_pending") as append_pending, patch(
            "builtins.open", side_effect=OSError("disk full")
        ):
            app.config["enable_nsfw"] = False
            gui.run_registration(1)

        self.assertEqual(gui.success_count, 0)
        self.assertEqual(gui.fail_count, 1)
        self.assertEqual(gui.results, [])
        add_to_cpa.assert_not_called()
        append_pending.assert_called_once()
        self.assertEqual(append_pending.call_args.args, ("a@example.com", "sso-token"))


if __name__ == "__main__":
    unittest.main()
