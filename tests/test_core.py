import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from spark import Ledger, SparkError, beijing_day, validate_config


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
