import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import dashboard
import notify
import spark
from reports import Report, read_json


class ReportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.directory = self.root / "data"
        self.directory.mkdir()
        self.topic = self.directory / "notify_url"
        self.topic.write_text("https://ntfy.sh/test-account-only")
        (self.directory / "dashboard_url").write_text("https://example.test/douyin/")
        self.config = {"friends": [f"测试好友{i}" for i in range(16)], "send_time": "23:00"}
        for p in (patch.object(notify, "ROOT", self.root),
                  patch.dict(os.environ, INVOCATION_ID="test-run", MONITOR_INVOCATION_ID="test-run")):
            p.start()
            self.addCleanup(p.stop)

    def test_historical_batches_and_time_changes(self):
        for schedule, normal, issue, priority in [
            ("23:00", "sent", "recipient", 3), ("11:00", "rejected", "recipient", 4),
            ("23:00", "sent", "unavailable", 3),
        ]:
            report = Report.start({**self.config, "send_time": schedule}, False, "")
            friends = list(report.results)
            report.results = dict.fromkeys(friends, normal)
            report.results[friends[-1]] = issue
            with patch.object(notify, "send_notice") as sender:
                notify.finish(report, self.directory, None)
                notify.service_failure("daily")
                sender.assert_called_once()
                url, title, body, actual = sender.call_args.args
                self.assertIn(schedule, title)
                self.assertIn("0/16" if normal == "rejected" else "15/16", title)
                self.assertEqual(actual, priority)
                self.assertNotIn(friends[-1], body)
                self.assertNotIn("重新登录", body)
                self.assertIn(report.report_id, sender.call_args.kwargs["click"])
                self.assertIn(friends[-1], dashboard.render_reports(self.directory, report.report_id))
                self.assertIn("已确认" if normal == "sent" else "页面显示发送失败",
                              dashboard.render_reports(self.directory, report.report_id))

    def test_checks_alert_only_on_supported_transitions(self):
        with patch.object(notify, "send_notice") as sender:
            for state in ("ok", "unknown", "login", "login", "unknown", "challenge", "ok", "ok"):
                report = Report.start(self.config, True, "")
                report.access = state
                report.results["测试好友0"] = "recipient"
                notify.finish(report, self.directory, self.topic)
                notify.service_failure("check")
            titles = [c.args[1] for c in sender.call_args_list]
            self.assertEqual(["抖音要求重新登录", "抖音要求安全验证", "抖音账号已恢复"], titles)

    def test_crash_fallback_ignores_old_reports_and_retries_unpublished_report(self):
        report = Report.start(self.config, False, "")
        report.save(self.directory)
        with patch.object(notify, "send_notice") as sender:
            notify.service_failure("daily")
            self.assertTrue(read_json(self.directory / "report-daily.json")["handled"])
            notify.service_failure("daily")
            with patch.dict(os.environ, MONITOR_INVOCATION_ID="different-run"):
                notify.service_failure("daily")
            self.assertEqual(2, sender.call_count)
            self.assertEqual("定时任务未能完成", sender.call_args.args[1])

    def test_member_detail_and_topic_stay_private(self):
        member = self.directory / "accounts" / "123456abcdef"
        member.mkdir(parents=True)
        topic = member / "notify_url"
        report = Report.start(self.config, False, "个人号")
        with patch.object(notify, "send_notice") as sender:
            notify.finish(report, member, topic)
            sender.assert_not_called()
            topic.write_text("https://ntfy.sh/test-private-only")
            notify.finish(report, member, topic)
            self.assertEqual(topic.read_text(), sender.call_args.args[0])
            self.assertIn("a=123456abcdef", sender.call_args.kwargs["click"])
            with self.assertRaises(ValueError):
                dashboard.render_reports(self.directory, report.report_id)

    def test_transport_has_no_recipient_names_and_uses_click_header(self):
        with patch.object(notify, "urlopen") as transport:
            transport.return_value.__enter__.return_value.status = 200
            notify.send_notice(self.topic.read_text(), "23:00 定时发送 · 15/16 已确认", "摘要", 3, "https://example.test/")
            request = transport.call_args.args[0]
            self.assertEqual("3", request.get_header("Priority"))
            self.assertEqual("https://example.test/", request.get_header("Click"))
            self.assertIn("=?utf-8?", request.get_header("Title").lower())
            self.assertEqual("摘要", request.data.decode())

    def test_chat_timeout_rechecks_read_only_once(self):
        page = Mock()
        page.goto.side_effect = spark.PlaywrightTimeout("timed out")
        with self.assertRaises(spark.SparkError) as err:
            spark.load_chat(page)
        self.assertEqual(2, page.goto.call_count)
        self.assertEqual("unavailable", err.exception.reason)
