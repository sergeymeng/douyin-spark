import json
import re
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, build_opener

import accounts
import dashboard
import notify
import setup_web


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for obj, key, value in (
            (accounts, "ROOT", self.root), (accounts, "REGISTRY", self.root/"data"/"accounts.json"),
            (notify, "ROOT", self.root), (setup_web, "ROOT", self.root),
            (setup_web, "ADMIN_PATH", self.root/"data"/"admin_auth.json"),
            (setup_web, "STATE", setup_web.SetupState()), (dashboard, "SIGNUP_ATTEMPTS", {}),
        ):
            self.enter_patch(obj, key, value)
        setup_web.STATE.serve = True
        setup_web.save_admin_password("admin-password")
        server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}/"

    def enter_patch(self, obj, key, value):
        patcher = patch.object(obj, key, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, client, path, **fields):
        data = urlencode(fields).encode() if fields else None
        with client.open(self.base + path, data=data, timeout=8) as response:
            return response.read().decode()

    def signup(self, name):
        client = build_opener(HTTPCookieProcessor(CookieJar()))
        page = self.request(client, "register", username=name, password="personal-pass", confirm="personal-pass")
        return client, re.search(r'name="account" value="([^"]+)"', page)[1], re.search(r'name="token" value="([^"]+)"', page)[1]

    def test_signup_login_and_personal_workspace(self):
        guest = build_opener()
        self.assertIn('href="register"', self.request(guest, "login"))
        self.assertIn("注册并登录", self.request(guest, "register"))
        alice, own, csrf = self.signup("Alice")
        bob, other, other_csrf = self.signup("Bob")
        self.assertNotEqual(csrf, other_csrf)
        self.assertNotEqual(own, other)
        self.assertNotIn("添加账号", self.request(alice, ""))
        self.assertNotIn("alice", self.request(bob, ""))
        admin = build_opener(HTTPCookieProcessor(CookieJar()))
        admin_page = self.request(admin, "login", username="admin", password="admin-password")
        self.assertIn("原账号", admin_page)
        self.assertNotIn("alice", admin_page)
        self.assertNotIn("bob", admin_page)
        for route in ("?a=main", f"api/status?a={other}", f"api/qr?a={other}"):
            with self.assertRaises(HTTPError) as err:
                self.request(alice, route)
            self.assertEqual(404, err.exception.code)
        for route in ("job", "toggle", "save", "notifications"):
            with self.assertRaises(HTTPError):
                self.request(alice, route, token=csrf, account=other)
        with self.assertRaises(HTTPError):
            self.request(alice, "add-account", token=csrf, name="second")
        with self.assertRaises(HTTPError):
            self.request(alice, "save", token=other_csrf, account=own)
        config, auth, directory = accounts.paths(own)
        auth.write_text("{}")
        self.request(alice, "save", token=csrf, account=own, mode="text", text="hi", friend="测试好友", send_time="11:00")
        self.assertEqual("11:00", json.loads(config.read_text())["send_time"])
        self.assertTrue(accounts.get_account(own)["enabled"])
        self.assertFalse(accounts.get_account(other)["enabled"])
        with patch.object(dashboard, "publish") as publish:
            self.request(alice, "notifications", token=csrf, account=own, notify_url="https://ntfy.sh/privatealice")
            publish.assert_called_once_with("test", topic_file=directory / "notify_url")
        self.assertNotIn("privatealice", self.request(bob, ""))
        self.request(alice, "logout", token=csrf)
        self.assertIn("注册新账号", self.request(alice, ""))
        with self.assertRaises(HTTPError):
            self.request(alice, "login", username="Alice", password="incorrect")
        self.assertIn("测试好友", self.request(alice, "login", username="ALICE", password="personal-pass"))

    def test_invalid_and_concurrent_duplicate_registrations(self):
        for username, password in (("ADMIN", "secure-pass"), ("a", "secure-pass"), ("user", "tiny")):
            with self.assertRaises(ValueError):
                accounts.register(username, password)
        def register_once(name):
            try:
                return accounts.register(name, "secure-pass")["id"]
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=2) as worker:
            result = list(worker.map(register_once, ["Same", "same"]))
        self.assertEqual(1, sum(value is not None for value in result))
        self.assertNotIn("secure-pass", accounts.REGISTRY.read_text())
        self.assertEqual(2, len(accounts.list_accounts()))
