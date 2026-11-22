"""Paths and registry for Douyin accounts managed by one installation."""

from __future__ import annotations

import hashlib
import hmac
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


def password_digest(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**15, r=8, p=1,
                          maxmem=128 * 1024 * 1024)


def register(username: str, password: str) -> dict:
    username = username.strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{3,32}", username) or username == "admin":
        raise ValueError("用户名需为 3 到 32 位字母、数字或下划线，且不能使用 admin")
    if not 8 <= len(password) <= 128:
        raise ValueError("密码需为 8 到 128 个字符")
    salt = secrets.token_bytes(16)
    credentials = {"username": username, "salt": salt.hex(),
                   "digest": password_digest(password, salt).hex()}
    return add_account(username, credentials=credentials)


def member_login(username: str, password: str) -> str | None:
    username = username.strip().lower()
    account = next((a for a in list_accounts()
                    if a.get("credentials", {}).get("username") == username), None)
    record = account["credentials"] if account else {}
    salt = bytes.fromhex(record.get("salt", "00" * 16))
    digest = password_digest(password, salt)
    if account and hmac.compare_digest(digest.hex(), record["digest"]):
        return account["id"]
    return None


def add_account(name: str, *, credentials: dict | None = None) -> dict:
    name = name.strip()
    if not 1 <= len(name) <= 40:
        raise ValueError("账号名称填 1 到 40 个字")
    with LOCK:
        accounts = list_accounts()
        account = {"id": secrets.token_hex(6), "name": name, "enabled": False,
                   "activated_at": beijing_now().isoformat()}
        if credentials:
            if any(a.get("credentials", {}).get("username") == credentials["username"] for a in accounts):
                raise ValueError("这个用户名已被使用")
            account["credentials"] = credentials
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
