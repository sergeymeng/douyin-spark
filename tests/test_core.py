import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from spark import Ledger, SparkError, beijing_day, run, validate_config


VALID = {
    "timezone": "Asia/Shanghai",
    "send_time": "20:30",
    "friends": ["小林", "阿晴"],
    "message": {"type": "text", "value": "今天怎么样？"},
}


class CoreTests(unittest.TestCase):
    def test_beijing_calendar_day(self):
        self.assertEqual("2026-10-16", beijing_day(datetime(2026, 10, 15, 16, 1, tzinfo=timezone.utc)))

    def test_duplicate_friend_rejected(self):
        config = {**VALID, "friends": ["小林", "小林"]}
        with self.assertRaises(SparkError):
            validate_config(config)

    def test_invalid_time_rejected(self):
        config = {**VALID, "send_time": "24:00"}
        with self.assertRaises(SparkError):
            validate_config(config)

    def test_submits_all_friends_before_reload_checks_and_never_resends(self):
        events = []

        def submit(page, friend, message):
            events.append(("submit", friend))
            return 3, {friend}

        def verify(page, friend, message, before, ids):
            events.append(("verify", friend))
            self.assertEqual((3, {friend}), (before, ids))
            if friend == "小林":
                raise SparkError("查不到回执")

        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            auth = directory / "auth.json"
            auth.write_text('{"cookies":[],"origins":[]}')
            with patch("spark.load_chat"), patch("spark.open_friend"), \
                 patch("spark.submit_message", side_effect=submit), \
                 patch("spark.verify_after_reload", side_effect=verify):
                self.assertEqual(1, run(VALID, auth, directory, False))
                self.assertEqual(0, run(VALID, auth, directory, False))
            self.assertEqual([("submit", "小林"), ("submit", "阿晴"),
                              ("verify", "小林"), ("verify", "阿晴")], events)
            db = sqlite3.connect(directory / "sends.sqlite3")
            try:
                self.assertEqual({"小林": "uncertain", "阿晴": "sent"},
                                 dict(db.execute("SELECT friend, status FROM sends")))
            finally:
                db.close()

    def test_once_per_friend_per_day(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = Ledger(Path(temp) / "test.sqlite3")
            try:
                self.assertTrue(ledger.claim("2026-10-15", "小林"))
                ledger.mark("2026-10-15", "小林", "sent")
                self.assertFalse(ledger.claim("2026-10-15", "小林"))
                self.assertTrue(ledger.claim("2026-10-15", "阿晴"))
                self.assertTrue(ledger.claim("2026-10-16", "小林"))
            finally:
                ledger.close()


if __name__ == "__main__":
    unittest.main()
