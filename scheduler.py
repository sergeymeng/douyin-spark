"""Run additional Douyin accounts when their Beijing send time arrives."""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from accounts import MAIN, beijing_now, list_accounts, paths
from spark import load_config, run
from notify import publish


LOGGER = logging.getLogger("spark.scheduler")


def due(account: dict, send_time: str, now: datetime) -> bool:
    hour, minute = map(int, send_time.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    activated = datetime.fromisoformat(account["activated_at"])
    return activated <= target <= now < target + timedelta(minutes=60)


def claim_dispatch(directory: Path, day: str) -> bool:
    path = directory / "dispatch.json"
    if path.exists() and json.loads(path.read_text(encoding="utf-8")).get("day") == day:
        return False
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                     prefix="dispatch-", delete=False) as file:
        json.dump({"day": day}, file)
        temp = Path(file.name)
    try:
        temp.chmod(0o600)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return True


def main() -> int:
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    now = beijing_now()
    errors = 0
    for account in list_accounts():
        if account["id"] == MAIN or not account["enabled"]:
            continue
        config_path, auth_path, directory = paths(account["id"])
        if not config_path.exists() or not auth_path.exists():
            continue
        config = load_config(config_path)
        if not due(account, config["send_time"], now):
            continue
        if not claim_dispatch(directory, now.date().isoformat()):
            continue
        LOGGER.info("开始发送：%s", account["name"])
        try:
            result = run(config, auth_path, directory, False, account["name"])
            errors += result
            if result:
                try:
                    publish("accounts", account=account["name"])
                except Exception:
                    LOGGER.exception("失败通知没有发出：%s", account["name"])
        except Exception:
            LOGGER.exception("发送失败：%s", account["name"])
            errors += 1
            try:
                publish("accounts", account=account["name"])
            except Exception:
                LOGGER.exception("失败通知没有发出：%s", account["name"])
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
