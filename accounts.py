"""Paths and registry for Douyin accounts managed by one installation."""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from spark import ROOT


REGISTRY = ROOT / "data" / "accounts.json"
LOCK = threading.Lock()
MAIN = "main"


def beijing_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _default() -> dict:
    return {"accounts": [{"id": MAIN, "name": "原账号", "enabled": True,
                          "activated_at": "2020-01-01T00:00:00+08:00"}]}


def list_accounts() -> list[dict]:
    if not REGISTRY.exists():
        return _default()["accounts"]
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    return data["accounts"]


def get_account(account_id: str) -> dict:
    if not re.fullmatch(r"main|[a-f0-9]{12}", account_id):
        raise ValueError("账号不存在")
    for account in list_accounts():
        if account["id"] == account_id:
            return account
    raise ValueError("账号不存在")


def paths(account_id: str) -> tuple[Path, Path, Path]:
    get_account(account_id)
    if account_id == MAIN:
        return ROOT / "config.json", ROOT / "auth.json", ROOT / "data"
    directory = ROOT / "data" / "accounts" / account_id
    return directory / "config.json", directory / "auth.json", directory


def _write_registry(accounts: list[dict]) -> None:
    REGISTRY.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=REGISTRY.parent,
                                     prefix="accounts-", delete=False) as file:
        json.dump({"accounts": accounts}, file, ensure_ascii=False, indent=2)
        file.write("\n")
        temp = Path(file.name)
    try:
        temp.chmod(0o600)
        os.replace(temp, REGISTRY)
    finally:
        temp.unlink(missing_ok=True)


def add_account(name: str) -> dict:
    name = name.strip()
    if not 1 <= len(name) <= 40:
        raise ValueError("账号名称填 1 到 40 个字")
    with LOCK:
        accounts = list_accounts()
        account = {"id": secrets.token_hex(6), "name": name, "enabled": False,
                   "activated_at": beijing_now().isoformat()}
        accounts.append(account)
        directory = ROOT / "data" / "accounts" / account["id"]
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        _write_registry(accounts)
        return account


def update_account(account_id: str, **changes: object) -> dict:
    if not set(changes) <= {"name", "enabled", "activated_at"}:
        raise ValueError("无效的账号设置")
    with LOCK:
        accounts = list_accounts()
        for account in accounts:
            if account["id"] == account_id:
                account.update(changes)
                _write_registry(accounts)
                return account
    raise ValueError("账号不存在")
