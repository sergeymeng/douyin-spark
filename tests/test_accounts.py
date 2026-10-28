import json
import threading
import tempfile
import time
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import accounts
import dashboard
import setup_web
from scheduler import claim_dispatch, due
from spark import SparkError


class AccountsTests(unittest.TestCase):
    def test_existing_account_keeps_original_paths_and_new_account_is_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(accounts, "ROOT", root), patch.object(accounts, "REGISTRY", root / "data" / "accounts.json"):
                root.joinpath("config.json").write_text("{}")
                self.assertEqual((root / "config.json", root / "auth.json", root / "data"), accounts.paths("main"))
                added = accounts.add_account("第二个号")
                config, auth, data = accounts.paths(added["id"])
                self.assertEqual(data / "config.json", config)
                self.assertEqual(data / "auth.json", auth)
                self.assertFalse(added["enabled"])
                self.assertEqual(2, len(accounts.list_accounts()))
                self.assertTrue(root.joinpath("config.json").exists())
                self.assertEqual(0o600, accounts.REGISTRY.stat().st_mode & 0o777)

    def test_new_account_starts_on_or_after_activation(self):
        tz = ZoneInfo("Asia/Shanghai")
        account = {"activated_at": "2026-10-29T12:00:00+08:00"}
        self.assertFalse(due(account, "11:59", datetime(2026, 10, 29, 12, 1, tzinfo=tz)))
        self.assertTrue(due(account, "12:01", datetime(2026, 10, 29, 12, 1, 20, tzinfo=tz)))
        self.assertFalse(due(account, "12:01", datetime(2026, 10, 29, 13, 1, tzinfo=tz)))

    def test_extra_account_dispatch_claimed_once(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.assertTrue(claim_dispatch(directory, "2026-10-29"))
            self.assertFalse(claim_dispatch(directory, "2026-10-29"))
            self.assertTrue(claim_dispatch(directory, "2026-10-30"))

    def test_dashboard_shows_both_accounts_without_mixing_selected_friends(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(accounts, "ROOT", root), patch.object(accounts, "REGISTRY", root / "data" / "accounts.json"):
                main_config = {"timezone": "Asia/Shanghai", "send_time": "23:59",
                               "friends": ["小林"], "message": {"type": "text", "value": "你好"}}
                root.joinpath("config.json").write_text(json.dumps(main_config))
                other = accounts.add_account("工作号")
                other_config = {**main_config, "friends": ["小陈"]}
                accounts.paths(other["id"])[0].write_text(json.dumps(other_config))
                first = dashboard.render_dashboard("main")
                second = dashboard.render_dashboard(other["id"])
                self.assertIn('value="小林" checked', first)
                self.assertNotIn('value="小陈"', first)
                self.assertIn('value="小陈" checked', second)
                self.assertNotIn('value="小林"', second)
                self.assertIn("检查登录", first)
                self.assertIn("重新登录", second)

    def test_expired_login_check_reports_relogin(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(accounts, "ROOT", root), patch.object(accounts, "REGISTRY", root / "data" / "accounts.json"), \
                 patch.object(dashboard, "load_chat", side_effect=SparkError("扫码登录")), \
                 patch.object(dashboard, "any_visible", return_value=True):
                root.joinpath("auth.json").write_text('{"cookies":[],"origins":[]}')
                job = dashboard.AccountJob("main", "check")
                job.thread.start()
                job.thread.join(timeout=15)
                self.assertFalse(job.thread.is_alive())
                self.assertEqual("expired", job.info()["state"])

    def test_relogin_shows_qr_and_saves_account_state(self):
        qr = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/aXcAAAAASUVORK5CYII="

        class PageHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = (f'<img src="data:image/png;base64,{qr}">'
                        '<script>setTimeout(() => { document.body.innerHTML = '
                        '\'<input placeholder="搜索好友"><div class="conversationConversationItemtitle">小林</div>\'; }, 2500)</script>')
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), PageHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                with patch.object(accounts, "ROOT", root), patch.object(accounts, "REGISTRY", root / "data" / "accounts.json"), \
                     patch.object(dashboard, "CHAT_URL", f"http://127.0.0.1:{server.server_address[1]}/chat"), \
                     patch.object(dashboard, "open_friend"), patch.object(dashboard, "sticker_names", return_value=["续火花"]):
                    job = dashboard.AccountJob("main", "login")
                    job.thread.start()
                    job.thread.join(timeout=20)
                    self.assertFalse(job.thread.is_alive())
                    self.assertEqual("ready", job.info()["state"])
                    self.assertTrue(job.info()["qr"])
                    self.assertTrue(root.joinpath("auth.json").exists())
                    self.assertEqual(0o600, root.joinpath("auth.json").stat().st_mode & 0o777)
                    self.assertEqual((["小林"], ["续火花"]), dashboard.CACHE["main"])
        finally:
            server.shutdown()
            server.server_close()

    def test_dashboard_http_requires_admin_login_and_adds_account(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            session = "test-session"
            try:
                with patch.object(accounts, "ROOT", root), patch.object(accounts, "REGISTRY", root / "data" / "accounts.json"), \
                     patch.object(setup_web, "admin_mode", return_value="password"), \
                     patch.object(setup_web.STATE, "sessions", {session: time.time() + 60}), \
                     patch.object(setup_web.STATE, "serve", True):
                    with self.assertRaises(HTTPError) as error:
                        urlopen(base + "/api/status?a=main")
                    self.assertEqual(401, error.exception.code)
                    headers = {"Cookie": f"spark_session={session}"}
                    with urlopen(Request(base + "/", headers=headers)) as response:
                        self.assertIn("添加账号", response.read().decode())
                    body = urlencode({"token": setup_web.STATE.token, "name": "第二个号"}).encode()
                    with urlopen(Request(base + "/add-account", data=body, headers=headers)) as response:
                        self.assertIn("第二个号", response.read().decode())
                    self.assertEqual(2, len(accounts.list_accounts()))
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
