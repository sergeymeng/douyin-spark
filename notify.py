"""Send delivery and failure notices to the configured ntfy topic."""

from __future__ import annotations

import sys
import time
import re
import os
import subprocess
import secrets
import tempfile
import threading
from email.header import Header
from urllib.parse import urlencode

from reports import Report, read_json, save_json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
LABELS = {
    "daily": "定时发送",
    "supplement": "补发",
    "check": "登录状态检查",
    "accounts": "多账号定时发送",
    "test": "通知测试",
    "success": "发送完成",
}


def validate_url(url: str) -> str:
    url = url.strip()
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.netloc != "ntfy.sh"
            or not re.fullmatch(r"/[A-Za-z0-9_-]{8,100}", parts.path)
            or parts.query or parts.fragment):
        raise ValueError("请输入 https://ntfy.sh/频道名")
    return url


def configured_url(path: Path | None = None) -> str:
    path = path or ROOT / "data" / "notify_url"
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


CHANNEL_LOCK = threading.Lock()


def configure_channel(path: Path, action: str, supplied: str = "") -> str:
    with CHANNEL_LOCK:
        if action == "remove":
            path.unlink(missing_ok=True)
            return "手机提醒已关闭。"
        current = configured_url(path)
        if action == "test-current":
            if not current:
                raise ValueError("请先生成频道。")
            url = current
        elif action in {"create", "test"}:
            url = validate_url((supplied.strip() if action == "test" else "") or current
                               or "https://ntfy.sh/douyin-spark-" + secrets.token_urlsafe(18))
        else:
            raise ValueError("无效的通知操作")
        if current != url:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".notify-", delete=False) as file:
                temp = Path(file.name)
                try:
                    file.write(url + "\n")
                    file.close()
                    os.replace(temp, path)
                finally:
                    temp.unlink(missing_ok=True)
        if action == "create":
            return "频道已准备好。复制下面的频道名，到 ntfy 里订阅。"
    try:
        send_notice(url, "定时发送 · 测试提醒", "收到这条消息，手机提醒就设好了。", 3)
    except RuntimeError:
        return "频道已保存，测试提醒没发出去。稍后可以再试。"
    return "测试提醒已发出，打开 ntfy 看看。"


def publish(kind: str, count: int = 0, account: str = "", *, topic_file: Path | None = None) -> bool:
    url = configured_url(topic_file)
    if not url:
        return False
    validate_url(url)
    if kind == "test":
        body = "服务器通知已接通。"
    elif kind == "success":
        body = f"{account or '抖音'}消息已发送给 {count} 位好友。"
    else:
        body = f"{account or '抖音'}{LABELS[kind]}未能完成，请查看任务记录。"
    return send_notice(url, "Douyin spark", body, 4 if kind not in {"test", "success"} else 3)


def send_notice(url: str, title: str, body: str, priority: int, click: str = "") -> bool:
    headers = {"Title": Header(title, "utf-8").encode(maxlinelen=998),
               "Priority": str(priority), "Tags": "warning" if priority == 4 else "information_source"}
    if click:
        headers["Click"] = click
    request = Request(
        validate_url(url), data=body.encode("utf-8"), headers=headers,
        method="POST",
    )
    for attempt in range(3):
        try:
            with urlopen(request, timeout=10) as response:
                if response.status != 200:
                    raise RuntimeError(f"通知服务返回 HTTP {response.status}")
            print("ntfy 已接受通知")
            return True
        except (HTTPError, URLError, TimeoutError) as exc:
            if attempt == 2:
                raise RuntimeError(f"通知发送失败：{exc}") from exc
            time.sleep(1 + attempt)


def finish(report: Report, directory: Path, topic_file: Path | None) -> None:
    report.save(directory)
    url = configured_url(topic_file)
    notice = report.summary() if report.kind == "daily" else None
    state_path = directory / "check-notice.json"
    if report.kind == "check":
        previous = read_json(state_path).get("access", "")
        notice = report.check_notice(previous)
    all_previously_handled = all(result.startswith("previous_") for result in report.results.values())
    if report.kind == "daily" and all_previously_handled and not report.problem:
        notice = None
    if url and notice:
        link_file = ROOT / "data" / "dashboard_url"
        link = link_file.read_text().strip() if link_file.is_file() else ""
        if link:
            query = {"report": report.report_id}
            if directory.parent.name == "accounts":
                query["a"] = directory.name
            link = link.rstrip("/") + "/?" + urlencode(query) + "#run-report"
        send_notice(url, *notice, click=link)
        if report.kind == "check":
            save_json(state_path, {"access": report.access})
    report.handled = True
    report.save(directory)


def service_failure(kind: str) -> None:
    directory = ROOT / "data"
    if kind in {"daily", "check"}:
        source = "douyin-spark" if kind == "daily" else "douyin-spark-check"
        invocation = os.getenv("MONITOR_INVOCATION_ID")
        if not invocation:
            proc = subprocess.run(["systemctl", "show", source + ".service", "-p", "InvocationID",
                                   "--value"], capture_output=True, text=True)
            invocation = proc.stdout.strip() if proc.returncode == 0 else ""
        record = read_json(directory / f"report-{kind}.json")
        if invocation and record.get("invocation") == invocation:
            if not record["handled"]:
                finish(Report(**record), directory, None)
            return
    url = configured_url()
    if url:
        send_notice(url, "定时任务未能完成", f"{LABELS[kind]}没有生成完整结果，请查看服务日志。", 4)


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in LABELS:
        raise SystemExit("用法：python notify.py daily|supplement|check|accounts|test")
    if sys.argv[1] in {"daily", "check", "supplement", "accounts"}:
        service_failure(sys.argv[1])
    else:
        publish(sys.argv[1])
