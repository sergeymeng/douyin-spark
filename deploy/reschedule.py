#!/usr/bin/env python3
"""Apply the sender's Beijing time from config.json to its systemd timer."""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    config_path = Path(sys.argv[1])
    status_path = Path(sys.argv[2])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    send_time = config["send_time"]
    if config.get("timezone") != "Asia/Shanghai" or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", send_time):
        raise ValueError("Invalid Beijing send time")
    calendar = f"*-*-* {send_time}:00 Asia/Shanghai"
    subprocess.run(["systemd-analyze", "calendar", calendar], check=True, stdout=subprocess.DEVNULL)

    override = Path("/etc/systemd/system/douyin-spark.timer.d/schedule.conf")
    override.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=override.parent,
                                     prefix="schedule-", delete=False) as temp:
        temp.write(f"[Timer]\nOnCalendar=\nOnCalendar={calendar}\n")
        temp_path = Path(temp.name)
    try:
        temp_path.chmod(0o644)
        os.replace(temp_path, override)
    finally:
        temp_path.unlink(missing_ok=True)
    subprocess.run(["systemctl", "daemon-reload"], check=True)

    status = {"send_time": send_time, "config_mtime_ns": config_path.stat().st_mtime_ns}
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=status_path.parent,
                                     prefix="schedule-status-", delete=False) as temp:
        json.dump(status, temp)
        status_temp = Path(temp.name)
    try:
        status_temp.chmod(0o644)
        os.replace(status_temp, status_path)
    finally:
        status_temp.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
