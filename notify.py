"""Send delivery and failure notices to the configured ntfy topic."""

from __future__ import annotations

import sys
import time
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
LABELS = {
    "daily": "每天 03:30 发送",
    "supplement": "10 月 16 日 07:00 补发",
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


def configured_url() -> str:
    path = ROOT / "data" / "notify_url"
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def publish(kind: str, count: int = 0, account: str = "") -> bool:
    url = configured_url()
    if not url:
        return False
    validate_url(url)
    if kind == "test":
        body = "服务器通知已接通。"
    elif kind == "success":
        body = f"{account or '抖音'}消息已发送给 {count} 位好友。"
    else:
        body = f"{account or '抖音'}{LABELS[kind]}失败。请检查是否需要重新登录，并查看服务器日志。"
    request = Request(
        url,
        data=body.encode("utf-8"),
        headers={"Title": "Douyin spark", "Priority": "4" if kind not in {"test", "success"} else "3",
                 "Tags": "warning" if kind not in {"test", "success"} else "white_check_mark"},
        method="POST",
    )
    for attempt in range(3):
        try:
            with urlopen(request, timeout=10) as response:
                if response.status != 200:
                    raise RuntimeError(f"通知服务返回 HTTP {response.status}")
            print(f"通知已送达：{kind}")
            return True
        except (HTTPError, URLError, TimeoutError) as exc:
            if attempt == 2:
                raise RuntimeError(f"通知发送失败：{exc}") from exc
            time.sleep(1 + attempt)


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in LABELS:
        raise SystemExit("用法：python notify.py daily|supplement|check|accounts|test")
    publish(sys.argv[1])
