"""Interactive local setup: scan Douyin QR, select chats, time, and message."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from spark import (CHAT_URL, ROOT, SparkError, open_friend, sticker_names,
                   validate_config, wait_for_chat_login)


def ask_time(default: str = "20:30") -> str:
    while True:
        value = input(f"每天北京时间几点发送？HH:MM [{default}]：").strip() or default
        try:
            validate_config({"timezone": "Asia/Shanghai", "send_time": value,
                             "friends": ["临时好友"],
                             "message": {"type": "text", "value": "测试"}})
            return value
        except SparkError:
            print("时间格式示例：20:30")


def collect_chat_names(page) -> list[str]:
    """Read every rendered section of Douyin's virtualized chat list."""
    deadline = time.monotonic() + 12
    names: list[str] = []
    while time.monotonic() < deadline:
        names = _visible_chat_names(page)
        if names and sum(not name.isdecimal() for name in names) >= len(names) / 2:
            break
        page.wait_for_timeout(600)

    scroll = page.locator(".conversationConversationListwrapper").first
    if not scroll.count():
        return [name for name in names if not name.isdecimal()]

    found: list[str] = []
    bottom_passes = 0
    for _ in range(200):
        before = len(found)
        for name in _visible_chat_names(page):
            if not name.isdecimal() and name not in found:
                found.append(name)
        top, height, viewport = scroll.evaluate(
            "el => [el.scrollTop, el.scrollHeight, el.clientHeight]"
        )
        at_bottom = top + viewport >= height - 2
        bottom_passes = bottom_passes + 1 if at_bottom and len(found) == before else 0
        if bottom_passes >= 3:
            break
        scroll.evaluate("(el, step) => { el.scrollTop += step }", max(250, viewport * 0.7))
        page.wait_for_timeout(600 if not at_bottom else 1000)
    scroll.evaluate("el => { el.scrollTop = 0 }")
    return found


def _visible_chat_names(page) -> list[str]:
    selectors = ('.conversationConversationItemtitle',
                 '.ConversationItemtitle',
                 '[data-e2e="conversation-item"] [class*="title"]')
    names: list[str] = []
    for selector in selectors:
        for node in page.locator(selector).all():
            value = node.inner_text(timeout=1000).strip()
            if value and value not in names:
                names.append(value)
        if names:
            break
    return names


def choose_friends(page) -> list[str]:
    names = [name for name in collect_chat_names(page) if not name.isdecimal()]
    if names:
        print("\n当前聊天列表：")
        for index, name in enumerate(names, 1):
            print(f"{index:>2}. {name}")
        print("输入序号，多个用逗号隔开；列表里没有的好友可直接输入准确备注名。")
    else:
        print("聊天列表暂时没有显示好友，请输入抖音聊天中的准确备注名。")
    while True:
        raw = input("选择好友：").strip()
        parts = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
        picked = []
        for part in parts:
            friend = names[int(part) - 1] if part.isdigit() and 1 <= int(part) <= len(names) else part
            if friend not in picked:
                picked.append(friend)
        if picked:
            return picked
        print("至少选一位好友。")


def choose_message(page, first_friend: str) -> dict:
    print("\n发送内容：1 文字；2 火花表情；3 其他原生表情")
    while True:
        mode = input("选择 1/2/3：").strip()
        if mode == "1":
            value = input("输入要发送的文字：").strip()
            if value:
                return {"type": "text", "value": value}
            print("文字不能为空。")
            continue
        if mode in {"2", "3"}:
            open_friend(page, first_friend)
            available = sticker_names(page)
            if available:
                print("可见的原生表情：")
                for index, name in enumerate(available, 1):
                    print(f"{index:>2}. {name}")
            default = "续火花" if mode == "2" else ""
            chosen = input(f"输入表情序号或准确名称{f' [{default}]' if default else ''}：").strip() or default
            value = available[int(chosen) - 1] if chosen.isdigit() and 1 <= int(chosen) <= len(available) else chosen
            if value:
                return {"type": "sticker", "value": value}
        print("请选择 1、2 或 3。")


def write_private_json(path: Path, value: dict) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write("\n")
    finally:
        os.chmod(path, 0o600)


def main() -> None:
    os.umask(0o077)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        try:
            context = browser.new_context(locale="zh-CN")
            page = context.new_page()
            page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=30000)
            print("浏览器已打开。请在抖音页面扫码登录，最多等待 5 分钟。")
            wait_for_chat_login(page, 300000)
            print("已进入聊天页。")
            friends = choose_friends(page)
            send_time = ask_time()
            message = choose_message(page, friends[0])
            config = validate_config({"timezone": "Asia/Shanghai", "send_time": send_time,
                                      "friends": friends, "message": message})
            write_private_json(ROOT / "config.json", config)
            context.storage_state(path=str(ROOT / "auth.json"))
            os.chmod(ROOT / "auth.json", 0o600)
            print("配置已保存。先运行 python spark.py --dry-run 检查好友定位。")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
