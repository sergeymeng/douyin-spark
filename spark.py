"""Send configured Douyin chat messages once per friend per Beijing day."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import re
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from playwright.sync_api import Browser, Locator, Page, TimeoutError as PlaywrightTimeout, sync_playwright


ROOT = Path(__file__).resolve().parent
CHAT_URL = "https://www.douyin.com/chat"
SEARCH = ('input[placeholder*="搜索"]', '[role="textbox"][placeholder*="搜索"]',
          'input[aria-label*="搜索"]')
SEARCH_ROWS = ('[class*="SearchPanelitembox"]', '[class*="SearchPanelitem-box"]')
CHAT_ROWS = ('[data-e2e="conversation-item"]', '.conversationConversationItemwrapper')
NAMES = ('[class*="SearchPanelitemtitle"]', '[class*="SearchPanelitemname"]',
         '[class*="conversationConversationItemtitle"]')
HEADERS = ('[class*="RightPanelHeader"]', '[class*="chatHeader"]')
EDITORS = ('[contenteditable="true"][data-slate-editor="true"]',
           '.messageEditorinputArea[contenteditable="true"]',
           '.DraftEditor-editor [contenteditable="true"]',
           '.DraftEditor-root [contenteditable="true"]',
           '[contenteditable="true"][data-placeholder*="发送消息"]',
           'textarea[placeholder*="消息"]')
SEND_BUTTONS = ('[class*="messageMsgInputpublishBtn"]', '.e2e-send-msg-bt',
                'button[aria-label*="发送"]')
OUTGOING = '.messageMessageBoxmessageBox:has(.messageMessageBoxisFromMe)'
STICKER_BUTTONS = ('button[aria-label*="表情"]', '[title*="表情"]',
                   'svg.messageMsgInputiconAction')
STICKER_PANELS = ('.componentsemojiemojiPanel', '[class*="emojiPanel"]')
LOGIN_PROMPTS = ('text=扫码登录', 'text=验证码登录', 'text=登录后')
CHALLENGES = ('text=安全验证', 'text=验证身份', 'text=完成验证')
FAILURE = ('[class*="ContentSideSendStatusretry"]', '[class*="SendStatusretry"]',
           '[aria-label*="重试"]', '[title*="重试"]')
PENDING = ('.semi-spin', '[class*="im-saas-message-spin"]', '[data-icon="spin"]')
LOGGER = logging.getLogger("spark")


class SparkError(RuntimeError):
    pass


def validate_config(config: object) -> dict:
    if not isinstance(config, dict):
        raise SparkError("config.json 必须是 JSON 对象")
    timezone = config.get("timezone")
    if timezone != "Asia/Shanghai":
        raise SparkError("本版本固定使用北京时间 Asia/Shanghai")
    send_time = config.get("send_time")
    if not isinstance(send_time, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", send_time):
        raise SparkError("send_time 必须是 00:00 到 23:59")
    friends = config.get("friends")
    if not isinstance(friends, list) or not friends or any(
        not isinstance(x, str) or not x.strip() or x != x.strip() for x in friends
    ) or len(friends) != len(set(friends)):
        raise SparkError("friends 要填不重复的、准确的好友备注名")
    message = config.get("message")
    if not isinstance(message, dict) or message.get("type") not in {"text", "sticker"}:
        raise SparkError("message.type 只能是 text 或 sticker")
    value = message.get("value")
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise SparkError("message.value 需为 1 到 200 字的内容或表情名称")
    return config


def load_config(path: Path = ROOT / "config.json") -> dict:
    try:
        return validate_config(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError as exc:
        raise SparkError(f"配置文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise SparkError(f"配置文件不是有效 JSON：{exc}") from exc


def beijing_day(now: datetime | None = None) -> str:
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    return now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()


@contextmanager
def exclusive_run(data_dir: Path):
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = data_dir / "run.lock"
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SparkError("已有一个发送任务在运行") from exc
        yield


class Ledger:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.execute("""CREATE TABLE IF NOT EXISTS sends (
            day TEXT NOT NULL, friend TEXT NOT NULL, status TEXT NOT NULL,
            updated_at TEXT NOT NULL, PRIMARY KEY(day, friend)
        )""")
        self.conn.commit()

    def claim(self, day: str, friend: str) -> bool:
        with self.conn:
            result = self.conn.execute(
                "INSERT OR IGNORE INTO sends VALUES (?, ?, 'attempted', ?)",
                (day, friend, datetime.now().astimezone().isoformat()),
            )
        return result.rowcount == 1

    def mark(self, day: str, friend: str, status: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE sends SET status=?, updated_at=? WHERE day=? AND friend=?",
                (status, datetime.now().astimezone().isoformat(), day, friend),
            )

    def close(self) -> None:
        self.conn.close()


def first_visible(page: Page, selectors: tuple[str, ...], timeout_ms: int = 15000) -> Locator:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        for selector in selectors:
            candidates = page.locator(selector)
            for index in range(min(candidates.count(), 8)):
                candidate = candidates.nth(index)
                if candidate.is_visible():
                    return candidate
        page.wait_for_timeout(250)
    raise SparkError("页面控件没有出现；请检查登录状态或网页是否更新")


def any_visible(page: Page, selectors: tuple[str, ...]) -> bool:
    for selector in selectors:
        item = page.locator(selector).first
        if item.count() and item.is_visible():
            return True
    return False


def wait_for_chat_login(page: Page, timeout_ms: int) -> None:
    deadline = time.monotonic() + timeout_ms / 1000
    chat_visible_at: float | None = None
    while time.monotonic() < deadline:
        if any_visible(page, SEARCH) and not any_visible(page, LOGIN_PROMPTS + CHALLENGES):
            chat_visible_at = chat_visible_at or time.monotonic()
            names = page.locator(".conversationConversationItemtitle").all_inner_texts()
            if names and sum(bool(name.strip()) and not name.strip().isdecimal()
                             for name in names) >= len(names) / 2:
                return
            if time.monotonic() - chat_visible_at >= 8:
                return
        page.wait_for_timeout(500)
    raise SparkError("还没有进入抖音聊天页；请扫码登录并完成页面提示")


def _exact_name_in(row: Locator, name: str) -> bool:
    for selector in NAMES:
        nodes = row.locator(selector)
        for index in range(min(nodes.count(), 8)):
            if _title_matches(nodes.nth(index).inner_text(timeout=1000), name):
                return True
    return False


def _title_matches(title: str, name: str) -> bool:
    title = title.strip()
    return title == name or re.sub(r"\s*\(\d+\)$", "", title) == name


def _matching_rows(page: Page, selectors: tuple[str, ...], name: str) -> list[Locator]:
    for selector in selectors:
        rows = page.locator(selector)
        matches = [row for index in range(min(rows.count(), 100))
                   if (row := rows.nth(index)).is_visible() and _exact_name_in(row, name)]
        if matches:
            return matches
    return []


def _find_in_chat_list(page: Page, name: str) -> list[Locator]:
    scroll = page.locator(".conversationConversationListwrapper").first
    if not scroll.count():
        return _matching_rows(page, CHAT_ROWS, name)
    scroll.evaluate("el => { el.scrollTop = 0 }")
    found_at: dict[str, float] = {}
    bottom_passes = 0
    for _ in range(200):
        matches = _matching_rows(page, CHAT_ROWS, name)
        if matches:
            top = scroll.evaluate("el => el.scrollTop")
            for match in matches:
                index = match.evaluate("el => el.parentElement.getAttribute('data-index')")
                found_at.setdefault(index or str(top), top)
        top, height, viewport = scroll.evaluate(
            "el => [el.scrollTop, el.scrollHeight, el.clientHeight]"
        )
        at_bottom = top + viewport >= height - 2
        bottom_passes = bottom_passes + 1 if at_bottom else 0
        if bottom_passes >= 3:
            break
        scroll.evaluate("(el, step) => { el.scrollTop += step }", max(250, viewport * 0.7))
        page.wait_for_timeout(450 if not at_bottom else 900)
    if len(found_at) != 1:
        return []
    scroll.evaluate("(el, top) => { el.scrollTop = top }", next(iter(found_at.values())))
    page.wait_for_timeout(250)
    return _matching_rows(page, CHAT_ROWS, name)


def open_friend(page: Page, name: str) -> None:
    search = first_visible(page, SEARCH)
    chat_list = page.locator(".conversationConversationListwrapper").first
    if chat_list.count() and not chat_list.is_visible():
        page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=30000)
        wait_for_chat_login(page, 20000)
        search = first_visible(page, SEARCH)
    matches = _matching_rows(page, CHAT_ROWS, name)
    if len(matches) != 1:
        search.fill(name.replace("\u00a0", " "))
        stable = 0
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            matches = _matching_rows(page, SEARCH_ROWS, name)
            stable = stable + 1 if len(matches) == 1 else 0
            if stable >= 2:
                break
            page.wait_for_timeout(300)
        if len(matches) != 1:
            page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=30000)
            wait_for_chat_login(page, 20000)
            matches = _find_in_chat_list(page, name)
    if len(matches) != 1:
        raise SparkError(f"好友“{name}”匹配到 {len(matches)} 个聊天；请使用唯一备注名")
    row = matches[0]
    button = row.locator('[class*="SearchPanelitemchat_btn"]').first
    (button if button.count() and button.is_visible() else row).click()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        for selector in HEADERS:
            heads = page.locator(selector)
            for index in range(min(heads.count(), 8)):
                head = heads.nth(index)
                if head.is_visible():
                    for title_selector in ('[class*="title"]', '[class*="name"]'):
                        titles = head.locator(title_selector)
                        if any(titles.nth(i).is_visible() and _title_matches(titles.nth(i).inner_text(), name)
                               for i in range(min(titles.count(), 12))):
                            first_visible(page, EDITORS, 3000)
                            return
        page.wait_for_timeout(300)
    raise SparkError(f"无法确认当前聊天属于“{name}”，已停止发送")


def _visible_inside(scope: Locator, selectors: tuple[str, ...]) -> bool:
    for selector in selectors:
        item = scope.locator(selector).first
        if item.count() and item.is_visible():
            return True
    return False


def _mark_existing(page: Page) -> None:
    page.locator(OUTGOING).evaluate_all(
        "nodes => nodes.forEach(n => n.setAttribute('data-spark-old', '1'))"
    )


def _confirm_new(page: Page, expected: str | None) -> None:
    deadline = time.monotonic() + 15
    found_at: float | None = None
    while time.monotonic() < deadline:
        fresh = page.locator(f"{OUTGOING}:not([data-spark-old])")
        for index in range(min(fresh.count(), 6)):
            bubble = fresh.nth(index)
            if expected is not None and expected not in bubble.inner_text(timeout=1000):
                continue
            if _visible_inside(bubble, FAILURE):
                raise SparkError("页面显示发送失败；今天不会自动重试")
            if _visible_inside(bubble, PENDING):
                found_at = None
                break
            found_at = found_at or time.monotonic()
            if time.monotonic() - found_at >= 2:
                return
        page.wait_for_timeout(300)
    raise SparkError("发送状态无法确认；今天不会自动重试，避免重复发消息")


def _editor_text(editor: Locator) -> str:
    return editor.inner_text(timeout=3000).translate(
        str.maketrans("", "", "\u200b\u200c\u200d\ufeff")
    ).strip()


def send_text(page: Page, value: str) -> None:
    editor = first_visible(page, EDITORS)
    tag = editor.evaluate("el => el.tagName.toLowerCase()")
    existing = editor.input_value().strip() if tag == "textarea" else _editor_text(editor)
    if existing or editor.locator("img").count():
        raise SparkError("聊天输入框里有未发送的草稿；请先手动处理")
    editor.click()
    if tag == "textarea":
        editor.fill(value)
    else:
        page.keyboard.insert_text(value)
    entered = editor.input_value() if tag == "textarea" else _editor_text(editor)
    if value not in entered:
        raise SparkError("文字没有进入聊天输入框")
    _mark_existing(page)
    for selector in SEND_BUTTONS:
        button = page.locator(selector).first
        if button.count() and button.is_visible():
            button.click()
            break
    else:
        editor.press("Enter")
    _confirm_new(page, value)


def sticker_names(page: Page) -> list[str]:
    button = first_visible(page, STICKER_BUTTONS, 5000)
    button.click()
    panel = first_visible(page, STICKER_PANELS, 5000)
    scroll = panel.locator(".componentsemojitabPanel").first
    names: list[str] = []
    bottom_passes = 0
    for _ in range(30):
        for value in panel.locator('[class*="emojiItemDesc"]').all_inner_texts():
            name = value.strip()
            if name and name not in names:
                names.append(name)
        if not scroll.count():
            break
        top, height, viewport = scroll.evaluate(
            "el => [el.scrollTop, el.scrollHeight, el.clientHeight]"
        )
        at_bottom = top + viewport >= height - 2
        bottom_passes = bottom_passes + 1 if at_bottom else 0
        if bottom_passes >= 2:
            break
        scroll.evaluate("(el, step) => { el.scrollTop += step }", max(100, viewport * 0.8))
        page.wait_for_timeout(250)
    if scroll.count():
        scroll.evaluate("el => { el.scrollTop = 0 }")
    return names


def send_sticker(page: Page, name: str) -> None:
    editor = first_visible(page, EDITORS)
    if _editor_text(editor) or editor.locator("img").count():
        raise SparkError("聊天输入框里有未发送的草稿；请先手动处理")
    button = first_visible(page, STICKER_BUTTONS, 5000)
    button.click()
    panel = first_visible(page, STICKER_PANELS, 5000)
    matching: list[Locator] = []
    scroll = panel.locator(".componentsemojitabPanel").first
    for _ in range(30):
        items = panel.locator('.emojiEmojiItememojiItem')
        matching = []
        for index in range(min(items.count(), 300)):
            item = items.nth(index)
            label = item.locator('[class*="emojiItemDesc"]').first
            if label.count() and label.inner_text().strip() == name and item.is_visible():
                matching.append(item)
        if matching or not scroll.count():
            break
        top, height, viewport = scroll.evaluate(
            "el => [el.scrollTop, el.scrollHeight, el.clientHeight]"
        )
        if top + viewport >= height - 2:
            break
        scroll.evaluate("(el, step) => { el.scrollTop += step }", max(100, viewport * 0.8))
        page.wait_for_timeout(250)
    if len(matching) != 1:
        raise SparkError(f"表情“{name}”匹配到 {len(matching)} 项；请重新选择")
    _mark_existing(page)
    matching[0].click()
    page.wait_for_timeout(1000)
    if not page.locator(f"{OUTGOING}:not([data-spark-old])").count():
        if editor.locator("img").count():
            for selector in SEND_BUTTONS:
                publish = page.locator(selector).first
                if publish.count() and publish.is_visible():
                    publish.click()
                    break
    _confirm_new(page, None)


def outgoing_count(page: Page, message: dict) -> int:
    outgoing = page.locator(OUTGOING)
    if message["type"] == "sticker":
        return outgoing.count()
    value = message["value"]
    return sum(value in text for text in outgoing.all_inner_texts())


def sticker_message_ids(page: Page) -> set[str]:
    sources = page.locator(f"{OUTGOING} .MessageItemEmojiimage").evaluate_all(
        "nodes => nodes.map(node => node.getAttribute('src')).filter(Boolean)"
    )
    return {message_id for source in sources
            if (message_id := parse_qs(urlsplit(source).query).get("l", [""])[0])}


def verify_after_reload(page: Page, friend: str, message: dict, before: int,
                        before_sticker_ids: set[str]) -> None:
    # A new bubble can appear before Douyin accepts it. Read the chat again from
    # the server before recording a successful send.
    page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=30000)
    wait_for_chat_login(page, 20000)
    open_friend(page, friend)
    page.wait_for_timeout(1000)
    deadline = time.monotonic() + 8
    found_at: float | None = None
    while time.monotonic() < deadline:
        new_sticker = (message["type"] == "sticker"
                       and bool(sticker_message_ids(page) - before_sticker_ids))
        if new_sticker or outgoing_count(page, message) > before:
            found_at = found_at or time.monotonic()
            if time.monotonic() - found_at >= 1:
                return
        else:
            found_at = None
        page.wait_for_timeout(400)
    raise SparkError("重新打开聊天后未找到新消息；今天不会自动重试")


def run(config: dict, auth: Path, data_dir: Path, dry_run: bool) -> int:
    if not auth.exists():
        raise SparkError("缺少 auth.json；先在本机运行 python configure.py 扫码登录")
    with exclusive_run(data_dir):
        ledger = Ledger(data_dir / "sends.sqlite3")
        try:
            with sync_playwright() as playwright:
                browser: Browser = playwright.chromium.launch(headless=True)
                try:
                    context = browser.new_context(storage_state=str(auth), locale="zh-CN")
                    page = context.new_page()
                    page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=30000)
                    wait_for_chat_login(page, 20000)
                    day = beijing_day()
                    errors = 0
                    sent_count = 0
                    for friend in config["friends"]:
                        claimed = False
                        try:
                            open_friend(page, friend)
                            if dry_run:
                                LOGGER.info("检查通过：%s", friend)
                                continue
                            if not ledger.claim(day, friend):
                                LOGGER.info("今天已尝试过：%s，跳过", friend)
                                continue
                            claimed = True
                            message = config["message"]
                            before = outgoing_count(page, message)
                            before_sticker_ids = (sticker_message_ids(page)
                                                  if message["type"] == "sticker" else set())
                            if message["type"] == "text":
                                send_text(page, message["value"])
                            else:
                                send_sticker(page, message["value"])
                            verify_after_reload(page, friend, message, before,
                                                before_sticker_ids)
                            ledger.mark(day, friend, "sent")
                            sent_count += 1
                            LOGGER.info("已发送：%s", friend)
                        except Exception as exc:
                            errors += 1
                            LOGGER.error("%s：%s", friend, exc)
                            if claimed:
                                ledger.mark(day, friend, "uncertain")
                    if dry_run and not errors and config["message"]["type"] == "sticker":
                        sticker = config["message"]["value"]
                        if sticker not in sticker_names(page):
                            LOGGER.error("抖音表情“%s”当前不可用", sticker)
                            errors += 1
                        else:
                            LOGGER.info("表情可用：%s", sticker)
                    context.storage_state(path=str(auth))
                    auth.chmod(0o600)
                    if not dry_run and not errors and sent_count:
                        try:
                            from notify import publish
                            publish("success", sent_count)
                        except Exception as exc:
                            LOGGER.error("成功通知发送失败：%s", exc)
                    return 1 if errors else 0
                finally:
                    browser.close()
        finally:
            ledger.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="抖音好友每日私信发送")
    parser.add_argument("--dry-run", action="store_true", help="检查登录与好友，不发送")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--auth", type=Path, default=ROOT / "auth.json")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.umask(0o077)
    try:
        return run(load_config(args.config), args.auth, args.data_dir, args.dry_run)
    except (SparkError, PlaywrightTimeout) as exc:
        LOGGER.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
