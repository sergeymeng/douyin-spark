"""Per-account snapshots and notification decisions for scheduled runs."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


LABEL = {
    "sent": "已确认", "checked": "检查通过",
    "previous_sent": "今天此前已确认", "previous_attempt": "今天已尝试，结果未确认",
    "recipient": "未能唯一定位好友", "ambiguous": "匹配到多个聊天",
    "draft": "输入框里有草稿，已跳过", "sticker": "所选表情不可用",
    "skipped": "提交前跳过", "rejected": "页面显示发送失败",
    "uncertain": "已尝试，结果未确认", "unavailable": "聊天页未能打开",
    "login": "抖音要求重新登录", "challenge": "抖音要求安全验证",
    "not_started": "未开始", "internal": "任务运行异常",
}
ACCOUNT_ACTION = {"login": "请打开管理页重新扫码。", "challenge": "请打开抖音完成安全验证。"}
SUCCESS = {"sent", "previous_sent"}


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def save_json(path: Path, data: dict) -> None:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".report-", delete=False) as file:
        temp = Path(file.name)
        try:
            json.dump(data, file, ensure_ascii=False)
            file.close()
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


@dataclass
class Report:
    kind: str
    started: str
    send_time: str
    account: str
    results: dict[str, str]
    invocation: str
    report_id: str
    access: str = "unknown"
    problem: str = ""
    handled: bool = False

    @classmethod
    def start(cls, config: dict, check: bool, account: str) -> Report:
        return cls("check" if check else "daily",
                   datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
                   config["send_time"], account, dict.fromkeys(config["friends"], "not_started"),
                   os.getenv("INVOCATION_ID", ""), uuid.uuid4().hex)

    def save(self, directory: Path) -> None:
        archive = directory / "reports"
        archive.mkdir(mode=0o700, exist_ok=True)
        save_json(archive / (self.report_id + ".json"), asdict(self))
        save_json(directory / f"report-{self.kind}.json", asdict(self))

    def summary(self) -> tuple[str, str, int]:
        counts = Counter(self.results.values())
        confirmed = sum(counts[code] for code in SUCCESS)
        total = len(self.results)
        title = f"{self.send_time} 定时发送 · {confirmed}/{total} 已确认"
        parts = [f"{n} 位{LABEL[code]}" for code, n in counts.items() if code not in SUCCESS]
        if self.problem:
            parts.append(LABEL[self.problem])
        if self.access in ACCOUNT_ACTION:
            parts.append(ACCOUNT_ACTION[self.access])
        elif counts["uncertain"] or counts["rejected"] or counts["previous_attempt"]:
            parts.append("请先核对聊天记录，避免重复补发。")
        elif counts["recipient"] or counts["ambiguous"]:
            parts.append("请在管理页核对好友备注。")
        if not parts:
            parts.append("本次名单中的好友均已确认发送。")
        if self.account:
            parts.insert(0, self.account)
        return title, "；".join(parts), 4 if confirmed == 0 or self.access in ACCOUNT_ACTION else 3

    def check_notice(self, previous: str) -> tuple[str, str, int] | None:
        if self.access == previous:
            return None
        if self.access in ACCOUNT_ACTION:
            return LABEL[self.access], ACCOUNT_ACTION[self.access], 4
        if self.access == "ok" and previous in ACCOUNT_ACTION:
            return "抖音账号已恢复", "已重新进入聊天页。", 3
        return None
