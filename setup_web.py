"""Browser setup page for local onboarding and remote management."""

from __future__ import annotations

import html
import os
import sys
import argparse
import base64
import getpass
import hashlib
import hmac
import json
import secrets
import tempfile
import threading
import time
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

from configure import collect_chat_names, write_private_json
from notify import configured_url, publish, validate_url
from spark import CHAT_URL, ROOT, open_friend, sticker_names, validate_config, wait_for_chat_login


EMOJI = ["🔥", "✨", "❤️", "😄", "👋", "🌙", "☀️", "🎉", "🥰", "💪"]
ADMIN_PATH = ROOT / "data" / "admin_auth.json"
SESSION_SECONDS = 12 * 60 * 60


class SetupState:
    def __init__(self) -> None:
        self.token = secrets.token_urlsafe(24)
        self.ready = False
        self.done = threading.Event()
        self.friends: list[str] = []
        self.stickers: list[str] = []
        self.error = ""
        self.prefill: dict = {}
        self.updated_at = 0.0
        self.scan_lock = threading.Lock()
        self.admin: dict = {}
        self.admin_stamp = -1
        self.sessions: dict[str, float] = {}
        self.failures: dict[str, list[float]] = {}
        self.auth_lock = threading.Lock()
        self.serve = False
        self.notice = ""


STATE = SetupState()


def save_admin_password(password: str) -> None:
    if not password:
        raise ValueError("密码不能为空")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=2**15, r=8, p=1, maxmem=128 * 1024 * 1024)
    ADMIN_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private_json(ADMIN_PATH, {
        "mode": "password",
        "username": "admin",
        "salt": base64.b64encode(salt).decode("ascii"),
        "digest": base64.b64encode(digest).decode("ascii"),
    })


def refresh_admin() -> None:
    try:
        stamp = ADMIN_PATH.stat().st_mtime_ns
    except FileNotFoundError:
        stamp = -1
    if stamp == STATE.admin_stamp:
        return
    with STATE.auth_lock:
        if stamp == STATE.admin_stamp:
            return
        STATE.admin = json.loads(ADMIN_PATH.read_text(encoding="utf-8")) if stamp != -1 else {}
        STATE.admin_stamp = stamp
        STATE.sessions.clear()


def admin_mode() -> str:
    refresh_admin()
    if not STATE.admin:
        return "unset"
    return STATE.admin.get("mode", "password")


def verify_admin(username: str, password: str) -> bool:
    record = STATE.admin
    salt = base64.b64decode(record["salt"])
    expected = base64.b64decode(record["digest"])
    actual = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=2**15, r=8, p=1, maxmem=128 * 1024 * 1024)
    return hmac.compare_digest(username, record["username"]) and hmac.compare_digest(actual, expected)


def prefill_from_config() -> dict:
    path = ROOT / "config.json"
    if not path.exists():
        return {}
    config = validate_config(json.loads(path.read_text(encoding="utf-8")))
    message = config["message"]
    value = message["value"]
    if message["type"] == "sticker":
        mode = "fire" if value == "续火花" else "sticker"
    else:
        mode = "emoji" if value in EMOJI else "text"
    return {"friends": config["friends"], "send_time": config["send_time"],
            "mode": mode, "text": value if mode == "text" else "",
            "emoji": value if mode == "emoji" else "🔥",
            "sticker": value if mode == "sticker" else ""}


def visible_chat_names() -> list[str]:
    chosen = set(STATE.prefill.get("friends", []))
    return [name for name in STATE.friends if not name.isdecimal() or name in chosen]


def wait_for_installed_timer(send_time: str, config_mtime_ns: int) -> None:
    """Wait for the separate systemd watcher to apply a changed send time."""
    if not Path("/etc/systemd/system/douyin-spark.timer").exists():
        return
    status_path = ROOT / "data" / "schedule_status.json"
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            if status.get("send_time") == send_time and status.get("config_mtime_ns") == config_mtime_ns:
                return
        except (FileNotFoundError, ValueError):
            pass
        time.sleep(0.2)
    raise TimeoutError("server timer did not pick up the new time")


STYLE = """
*{box-sizing:border-box}[hidden]{display:none!important}
body{margin:0;background:#f8f8f7;color:#252525;font:14px/1.5 -apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif}
button,input,select{font:inherit}button{cursor:pointer}
.page{max-width:720px;margin:52px auto 80px;padding:0 24px}
header{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:22px}
h1{font-size:26px;letter-spacing:-.025em;line-height:1.2;margin:0;font-weight:650}
header p{color:#777;margin:0;font-size:13px}.refresh{border:0;background:none;padding:0;color:#555;text-decoration:underline;text-underline-offset:3px;font-size:13px}.refresh:disabled{color:#999;cursor:wait}.refresh-status{min-height:18px;color:#777;font-size:12px;margin:-10px 0 14px}
form,.state{background:#fff;border:1px solid #dededb;border-radius:10px}
section{padding:24px 28px;border-bottom:1px solid #e9e9e6}
.section-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:15px}
h2{font-size:16px;font-weight:650;margin:0}.hint,.count{font-size:12px;color:#777}
.search,.field input,.field select,.time{height:40px;border:1px solid #d7d7d3;border-radius:6px;background:#fff;padding:0 11px;outline:none;color:#252525}
.search:focus,.field input:focus,.field select:focus,.time:focus{border-color:#555}
.search{width:100%;margin-bottom:10px}
.friend-list{border:1px solid #e2e2de;border-radius:6px;max-height:380px;overflow-y:auto;scrollbar-width:thin;display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}
.friend{height:38px;min-width:0;display:flex;align-items:center;gap:9px;padding:0 10px;border-bottom:1px solid #ededeb;cursor:pointer}
.friend:nth-child(odd){border-right:1px solid #ededeb}.friend:hover{background:#f8f8f7}.friend:has(input:checked){background:#f2f3f1}
.friend input{width:15px;height:15px;margin:0;accent-color:#303330;flex:none}.friend span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.empty{padding:15px;color:#777;grid-column:1/-1}.field{display:block;font-size:13px;margin-top:14px;color:#4f4f4d}
.field input,.field select{display:block;width:100%;margin-top:6px;color:#252525}
.when{display:flex;align-items:center;gap:10px}.time{width:105px;font-size:18px;font-weight:600;text-align:center}.tz{color:#777;font-size:12px;margin-left:4px}
.modes{display:flex;flex-wrap:wrap;gap:8px}.mode{border:1px solid #d7d7d3;border-radius:6px;padding:8px 12px;cursor:pointer;white-space:nowrap}
.mode input{accent-color:#303330;margin:0 6px 0 0;vertical-align:-1px}.mode:has(input:checked){border-color:#323532;background:#f4f5f3}.mode:has(input:disabled){opacity:.45;cursor:default}
footer{padding:20px 28px;display:flex;align-items:center;gap:14px}.save{border:0;border-radius:6px;background:#252825;color:white;padding:10px 25px;font-weight:600}.save:hover{background:#414541}
.error{margin:0 0 15px;padding:10px 12px;background:#fff1ee;border:1px solid #ebc8c0;border-radius:6px;color:#9a392a}
.state{padding:28px}.state p{color:#777;margin:8px 0 0}
.login-page{max-width:390px;margin:15vh auto;padding:0 20px}.login-page h1{margin-bottom:22px}
.login{padding:28px}.login label{display:block;margin-bottom:16px;color:#555;font-size:13px}
.login input{display:block;width:100%;height:42px;margin-top:6px;border:1px solid #d7d7d3;border-radius:6px;padding:0 11px;outline:none}
.login input:focus{border-color:#555}.login .save{width:100%;margin-top:5px}
.login-actions{display:flex;align-items:center;gap:16px}.login-actions .save{width:auto}.plain{border:0;background:none;color:#555;text-decoration:underline;text-underline-offset:3px;padding:0}
.notify-actions{display:flex;gap:16px;align-items:center;margin-top:16px}.notify-actions .save{padding:9px 16px}
.notice{margin:0 0 15px;padding:10px 12px;background:#eff6ef;border:1px solid #c9ddc9;border-radius:6px;color:#285b32}
.header-actions{display:flex;align-items:center;gap:18px}.logout{border:0;background:none;padding:0}.logout button{border:0;background:none;color:#777;font-size:13px;padding:0}
@media(max-width:600px){.page{margin:25px auto;padding:0 12px}header{padding:0 4px}header p{font-size:12px}section{padding:20px 16px}footer{padding:18px 16px}.friend-list{grid-template-columns:1fr;max-height:none;overflow:visible}.friend:nth-child(odd){border-right:0}.when{flex-wrap:wrap}}
"""


SCRIPT = """
const form = document.querySelector('form[action="save"]');
const search = document.querySelector('#friend-search');
const status = document.querySelector('#refresh-status');
const refreshButton = document.querySelector('#refresh');
function update() {
  const picked = [...document.querySelectorAll('input[name="friend"]:checked')].map(x => x.value);
  const extra = document.querySelector('input[name="other_friends"]').value.split(/[,，]/).map(x => x.trim()).filter(Boolean);
  const count = new Set([...picked, ...extra]).size;
  document.querySelector('#friend-count').textContent = `已选 ${count} 人`;
  const mode = document.querySelector('input[name="mode"]:checked')?.value || 'text';
  document.querySelectorAll('[data-for]').forEach(el => el.hidden = el.dataset.for !== mode);
}
function filterFriends() {
  const q = search.value.trim().toLocaleLowerCase();
  document.querySelectorAll('.friend').forEach(row => row.hidden = !row.dataset.name.toLocaleLowerCase().includes(q));
}
function showFriends(names) {
  const selected = [...document.querySelectorAll('input[name="friend"]:checked')].map(x => x.value);
  const list = document.querySelector('.friend-list');
  list.replaceChildren();
  for (const name of new Set([...names, ...selected])) {
    const label = document.createElement('label');
    label.className = 'friend'; label.dataset.name = name;
    const input = document.createElement('input');
    input.type = 'checkbox'; input.name = 'friend'; input.value = name;
    input.checked = selected.includes(name);
    const span = document.createElement('span');
    span.title = name; span.textContent = name;
    label.append(input, span); list.append(label);
  }
  filterFriends(); update();
}
function showStickers(names) {
  const select = document.querySelector('select[name="sticker"]');
  const selected = select.value;
  select.replaceChildren();
  for (const name of names) {
    const option = document.createElement('option');
    option.value = name; option.textContent = name; select.append(option);
  }
  if (names.includes(selected)) select.value = selected;
  const fire = document.querySelector('input[name="mode"][value="fire"]');
  const sticker = document.querySelector('input[name="mode"][value="sticker"]');
  fire.disabled = !names.includes('续火花');
  sticker.disabled = names.length === 0;
  if ((fire.checked && fire.disabled) || (sticker.checked && sticker.disabled)) {
    document.querySelector('input[name="mode"][value="text"]').checked = true;
  }
  update();
}
async function refreshData(force = false) {
  refreshButton.disabled = true; status.textContent = '正在更新…';
  try {
    const token = form.querySelector('input[name="token"]').value;
    const response = await fetch(`api/data?token=${encodeURIComponent(token)}&force=${force ? 1 : 0}`, {cache:'no-store'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '更新失败');
    showFriends(data.friends); showStickers(data.stickers);
    status.textContent = `已更新：${data.friends.length} 个聊天，${data.stickers.length} 个抖音表情`;
  } catch (error) {
    status.textContent = error.message;
  } finally {
    refreshButton.disabled = false;
  }
}
search.addEventListener('input', filterFriends);
refreshButton.addEventListener('click', () => refreshData(true));
form.addEventListener('input', update);
form.addEventListener('change', update);
form.addEventListener('submit', e => {
  if (!document.querySelector('input[name="friend"]:checked') && !document.querySelector('input[name="other_friends"]').value.trim()) {
    e.preventDefault(); search.focus(); alert('先选一位好友');
  }
});
update();
refreshData();
setInterval(refreshData, 5 * 60 * 1000);
"""


def render_login(error: str = "") -> str:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>登录 · 定时发送</title><style>{STYLE}</style></head><body><main class="login-page"><h1>定时发送</h1><form class="login" method="post" action="login"><label>账号<input name="username" autocomplete="username" autofocus required></label><label>密码<input name="password" type="password" autocomplete="current-password" required></label>{f'<p class="error">{html.escape(error)}</p>' if error else ''}<button class="save" type="submit">登录</button></form></main></body></html>"""


def render_first_run(error: str = "") -> str:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>初次设置 · 定时发送</title><style>{STYLE}</style></head><body><main class="login-page"><h1>先设一下访问方式</h1><form class="login" method="post" action="setup-auth"><input type="hidden" name="token" value="{STATE.token}"><p class="hint">账号固定为 admin。本机使用可以跳过密码；公网访问需要密码。</p><label>密码<input name="password" type="password" autocomplete="new-password"></label><label>再输入一次<input name="confirm" type="password" autocomplete="new-password"></label>{f'<p class="error">{html.escape(error)}</p>' if error else ''}<div class="login-actions"><button class="save" type="submit" name="choice" value="password">设置密码</button><button class="plain" type="submit" name="choice" value="skip" formnovalidate>跳过</button></div></form></main></body></html>"""


def render_page() -> str:
    if not STATE.ready:
        body = "<div class='state'><h2>等待抖音登录</h2><p>在打开的抖音窗口扫码，完成后这里会出现聊天列表。</p></div><meta http-equiv='refresh' content='2'>"
    elif STATE.done.is_set():
        body = "<div class='state'><h2>已保存</h2><p>设置已写入 config.json。可以关闭此页面。</p></div>"
    else:
        chosen = set(STATE.prefill.get("friends", []))
        visible_names = visible_chat_names()
        names = "".join(
            f'<label class="friend" data-name="{html.escape(name, quote=True)}"><input type="checkbox" name="friend" value="{html.escape(name, quote=True)}" {"checked" if name in chosen else ""}><span title="{html.escape(name, quote=True)}">{html.escape(name)}</span></label>'
            for name in visible_names
        ) or "<div class='empty'>没找到可显示的昵称。可以在下面手动填写备注名。</div>"
        sticker_options = "".join(
            f'<option value="{html.escape(name, quote=True)}" {"selected" if name == STATE.prefill.get("sticker") else ""}>{html.escape(name)}</option>'
            for name in STATE.stickers
        )
        emoji_options = "".join(
            f'<option value="{html.escape(item, quote=True)}" {"selected" if item == STATE.prefill.get("emoji") else ""}>{item}</option>'
            for item in EMOJI
        )
        has_fire = "续火花" in STATE.stickers
        default_mode = STATE.prefill.get("mode") or ("fire" if has_fire else "text")
        if default_mode not in {"text", "emoji", "sticker", "fire"}:
            default_mode = "text"
        default_time = html.escape(STATE.prefill.get("send_time", "20:30"), quote=True)
        default_text = html.escape(STATE.prefill.get("text", "今天怎么样？"), quote=True)
        other_friends = html.escape(STATE.prefill.get("other_friends", ""), quote=True)
        fire_mode = f'<label class="mode"><input type="radio" name="mode" value="fire" {"checked" if default_mode == "fire" else ""} {"" if has_fire else "disabled"}>续火花表情</label>'
        sticker_mode = f'<label class="mode"><input type="radio" name="mode" value="sticker" {"checked" if default_mode == "sticker" else ""} {"" if STATE.stickers else "disabled"}>其他抖音表情</label>'
        body = f"""
        {f'<p class="error">{html.escape(STATE.error)}</p>' if STATE.error else ''}
        {f'<p class="notice">{html.escape(STATE.notice)}</p>' if STATE.notice else ''}
        <div class="refresh-status" id="refresh-status" aria-live="polite"></div>
        <form method="post" action="save">
          <input type="hidden" name="token" value="{STATE.token}">
          <section>
            <div class="section-head"><h2>好友</h2><span class="count" id="friend-count"></span></div>
            <input class="search" id="friend-search" type="search" aria-label="搜索好友" placeholder="搜索昵称">
            <div class="friend-list">{names}</div>
            <label class="field">列表里没有？输入准确备注名<input name="other_friends" value="{other_friends}" placeholder="多个名字用逗号隔开"></label>
          </section>
          <section>
            <div class="section-head"><h2>时间</h2><span class="hint">每天</span></div>
            <div class="when"><input class="time" type="text" inputmode="numeric" name="send_time" value="{default_time}" placeholder="HH:MM" pattern="([01][0-9]|2[0-3]):[0-5][0-9]" title="24 小时制，例如 20:30" aria-label="发送时间，24 小时制" required><span>北京时间</span><span class="tz">24 小时制</span></div>
          </section>
          <section>
            <div class="section-head"><h2>内容</h2></div>
            <div class="modes">
              {fire_mode}
              <label class="mode"><input type="radio" name="mode" value="text" {"checked" if default_mode == "text" else ""}>文字</label>
              <label class="mode"><input type="radio" name="mode" value="emoji" {"checked" if default_mode == "emoji" else ""}>普通表情</label>
              {sticker_mode}
            </div>
            <label class="field" data-for="text">要发送的文字<input name="text" maxlength="200" value="{default_text}"></label>
            <label class="field" data-for="emoji">选择表情<select name="emoji">{emoji_options}</select></label>
            <label class="field" data-for="sticker">选择抖音表情<select name="sticker">{sticker_options}</select></label>
          </section>
          <footer><button class="save" type="submit">保存</button><span class="hint">保存后不会立即发送</span></footer>
        </form>
        <form method="post" action="notifications" class="notification-form">
          <input type="hidden" name="token" value="{STATE.token}">
          <section>
            <div class="section-head"><h2>通知</h2><span class="hint">ntfy</span></div>
            <label class="field">频道地址<input type="url" name="notify_url" value="{html.escape(configured_url(), quote=True)}" placeholder="https://ntfy.sh/你的频道" autocomplete="off"></label>
            <p class="hint">留空会生成新地址。手机 ntfy 应用订阅同一个频道；发送成功或出错时会推送。</p>
            <div class="notify-actions"><button class="save" type="submit" name="action" value="test">保存并测试</button><button class="plain" type="submit" name="action" value="remove" formnovalidate>取消绑定</button></div>
          </section>
        </form>
        <script>{SCRIPT}</script>
        """
    action = '<button type="button" class="refresh" id="refresh">更新好友与表情</button>' if STATE.ready and not STATE.done.is_set() else '<p>抖音私信</p>'
    account = '<form class="logout" method="post" action="logout"><button type="submit">退出</button></form>' if admin_mode() == "password" else ''
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>定时发送</title><style>{STYLE}</style></head><body><main class="page"><header><h1>定时发送</h1><div class="header-actions">{action}{account}</div></header>{body}</main></body></html>"""


def refresh_from_auth(force: bool = False) -> None:
    with STATE.scan_lock:
        if not force and time.monotonic() - STATE.updated_at < 300:
            return
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(storage_state=str(ROOT / "auth.json"), locale="zh-CN")
                page = context.new_page()
                page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=30000)
                wait_for_chat_login(page, 20000)
                friends = collect_chat_names(page)
                stickers: list[str] = []
                if friends:
                    open_friend(page, friends[0])
                    stickers = sticker_names(page)
                context.storage_state(path=str(ROOT / "auth.json"))
                (ROOT / "auth.json").chmod(0o600)
                STATE.friends = friends
                STATE.stickers = stickers
                STATE.updated_at = time.monotonic()
            finally:
                browser.close()


class Handler(BaseHTTPRequestHandler):
    def session_token(self) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        return cookie["spark_session"].value if "spark_session" in cookie else ""

    def authenticated(self) -> bool:
        mode = admin_mode()
        if mode == "none":
            return not STATE.serve
        if mode != "password":
            return False
        token = self.session_token()
        if not token:
            return False
        with STATE.auth_lock:
            expires = STATE.sessions.get(token, 0)
            if expires <= time.time():
                STATE.sessions.pop(token, None)
                return False
            return True

    def redirect(self, location: str, cookie: str = "") -> None:
        self.send_response(303)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def login(self) -> None:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if size < 0 or size > 4096:
            self.send_error(413)
            return
        form = parse_qs(self.rfile.read(size).decode("utf-8", errors="replace"))
        username = form.get("username", [""])[0]
        password = form.get("password", [""])[0]
        forwarded = self.headers.get("X-Forwarded-For", "") if STATE.serve else ""
        address = forwarded.rsplit(",", 1)[-1].strip() if forwarded else self.client_address[0]
        now = time.time()
        with STATE.auth_lock:
            failures = [stamp for stamp in STATE.failures.get(address, []) if now - stamp < 300]
            STATE.failures[address] = failures
            locked = len(failures) >= 5
        if locked:
            self.respond(render_login("登录尝试太多，请五分钟后再试。"), status=429)
            return
        if not verify_admin(username, password):
            with STATE.auth_lock:
                STATE.failures[address].append(now)
            self.respond(render_login("账号或密码不对。"), status=401)
            return
        token = secrets.token_urlsafe(32)
        with STATE.auth_lock:
            STATE.sessions[token] = now + SESSION_SECONDS
            STATE.failures.pop(address, None)
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        self.redirect("./", f"spark_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}{secure}")

    def setup_auth(self) -> None:
        if STATE.serve or self.server.server_address[0] != "127.0.0.1" or admin_mode() != "unset":
            self.send_error(403)
            return
        size = int(self.headers.get("Content-Length", "0"))
        if size < 0 or size > 4096:
            self.send_error(413)
            return
        form = parse_qs(self.rfile.read(size).decode("utf-8"), keep_blank_values=True)
        if form.get("token", [""])[0] != STATE.token:
            self.send_error(403)
            return
        choice = form.get("choice", [""])[0]
        if choice == "skip":
            ADMIN_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_private_json(ADMIN_PATH, {"mode": "none"})
            refresh_admin()
            self.redirect("./")
        elif choice == "password":
            password = form.get("password", [""])[0]
            if len(password) < 8:
                self.respond(render_first_run("密码至少 8 位。"), status=400)
            elif password != form.get("confirm", [""])[0]:
                self.respond(render_first_run("两次密码不一致。"), status=400)
            else:
                save_admin_password(password)
                refresh_admin()
                self.redirect("login")
        else:
            self.send_error(400)

    def save_notifications(self) -> None:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if size < 0 or size > 4096:
            self.send_error(413)
            return
        form = parse_qs(self.rfile.read(size).decode("utf-8", errors="replace"), keep_blank_values=True)
        if form.get("token", [""])[0] != STATE.token:
            self.send_error(403)
            return
        path = ROOT / "data" / "notify_url"
        action = form.get("action", [""])[0]
        STATE.error = STATE.notice = ""
        if action == "remove":
            path.unlink(missing_ok=True)
            STATE.notice = "已取消 ntfy 通知。"
        elif action == "test":
            try:
                supplied = form.get("notify_url", [""])[0].strip()
                url = validate_url(supplied or "https://ntfy.sh/douyin-spark-" + secrets.token_urlsafe(18))
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                                 prefix="notify-", delete=False) as temp:
                    temp.write(url + "\n")
                    temp_path = Path(temp.name)
                try:
                    temp_path.chmod(0o600)
                    os.replace(temp_path, path)
                finally:
                    temp_path.unlink(missing_ok=True)
                publish("test")
                STATE.notice = "已绑定，测试通知已发送。"
            except Exception as exc:
                STATE.error = f"通知没有发出去：{exc}"
        else:
            self.send_error(400)
            return
        self.respond(render_page(), status=400 if STATE.error else 200)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        mode = admin_mode()
        if STATE.serve and mode != "password":
            self.respond("<h1>请先在服务器上设置管理密码</h1>", status=503)
            return
        if mode == "unset":
            self.respond(render_first_run()) if url.path == "/" else self.redirect("./")
            return
        if url.path == "/login":
            self.redirect("./") if self.authenticated() else self.respond(render_login())
            return
        if not self.authenticated():
            if url.path == "/api/data":
                self.respond_json({"error": "请先登录"}, status=401)
            else:
                self.redirect("login")
            return
        if url.path == "/":
            self.respond(render_page())
        elif url.path == "/api/data" and STATE.ready and not STATE.done.is_set():
            args = parse_qs(url.query)
            if args.get("token", [""])[0] != STATE.token:
                self.send_error(403)
                return
            try:
                refresh_from_auth(force=args.get("force", [""])[0] == "1")
                self.respond_json({"friends": visible_chat_names(), "stickers": STATE.stickers})
            except Exception as exc:
                self.respond_json({"error": str(exc)}, status=500)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path == "/setup-auth":
            self.setup_auth()
            return
        if STATE.serve and admin_mode() != "password":
            self.send_error(503)
            return
        if self.path == "/login":
            self.login() if admin_mode() == "password" else self.send_error(404)
            return
        if not self.authenticated():
            self.send_error(401)
            return
        if self.path == "/logout":
            with STATE.auth_lock:
                STATE.sessions.pop(self.session_token(), None)
            self.redirect("login", "spark_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
            return
        if self.path == "/notifications":
            self.save_notifications()
            return
        if self.path != "/save" or not STATE.ready or STATE.done.is_set():
            self.send_error(404)
            return
        STATE.notice = ""
        size = int(self.headers.get("Content-Length", "0"))
        if size > 10000:
            self.send_error(413)
            return
        form = parse_qs(self.rfile.read(size).decode("utf-8"), keep_blank_values=True)
        if form.get("token", [""])[0] != STATE.token:
            self.send_error(403)
            return
        friends = list(dict.fromkeys(form.get("friend", []) + [
            x.strip() for x in form.get("other_friends", [""])[0].replace("，", ",").split(",") if x.strip()
        ]))
        mode = form.get("mode", [""])[0]
        STATE.prefill = {
            "friends": form.get("friend", []),
            "other_friends": form.get("other_friends", [""])[0],
            "send_time": form.get("send_time", [""])[0],
            "mode": mode,
            "text": form.get("text", [""])[0],
            "emoji": form.get("emoji", [""])[0],
            "sticker": form.get("sticker", [""])[0],
        }
        if mode == "text":
            message = {"type": "text", "value": form.get("text", [""])[0].strip()}
        elif mode == "emoji":
            message = {"type": "text", "value": form.get("emoji", [""])[0]}
        elif mode == "fire" and "续火花" in STATE.stickers:
            message = {"type": "sticker", "value": "续火花"}
        elif mode == "sticker" and STATE.stickers:
            message = {"type": "sticker", "value": form.get("sticker", [""])[0]}
        else:
            STATE.error = "请选择一种可用的发送内容。"
            self.respond(render_page(), status=400)
            return
        if message["type"] == "sticker" and message["value"] not in STATE.stickers:
            STATE.error = "表情名称不在当前可选列表。"
            self.respond(render_page(), status=400)
            return
        try:
            config = validate_config({"timezone": "Asia/Shanghai",
                                      "send_time": form.get("send_time", [""])[0],
                                      "friends": friends, "message": message})
        except Exception as exc:
            STATE.error = str(exc)
            self.respond(render_page(), status=400)
            return
        config_path = ROOT / "config.json"
        old_config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else None
        old_time = old_config.get("send_time") if old_config else None
        new_time = config["send_time"]
        timer_changed = STATE.serve and old_time != new_time
        try:
            write_private_json(config_path, config)
            if timer_changed:
                wait_for_installed_timer(new_time, config_path.stat().st_mtime_ns)
        except Exception:
            if old_config is not None:
                write_private_json(config_path, old_config)
            else:
                config_path.unlink(missing_ok=True)
            STATE.error = "设置没有保存，请检查服务器定时器。"
            self.respond(render_page(), status=500)
            return
        STATE.error = ""
        if STATE.serve:
            STATE.notice = "已保存，下次发送按新设置执行。"
        else:
            STATE.done.set()
        self.respond(render_page())

    def respond(self, body: str, status: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def respond_json(self, value: dict, status: int = 200) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--set-admin-password", action="store_true", help="设置或重设管理密码后退出")
    parser.add_argument("--reuse-auth", action="store_true", help="复用已保存的扫码状态")
    parser.add_argument("--no-open", action="store_true", help="不自动打开本机设置页")
    parser.add_argument("--port", type=int, default=0, help="本机监听端口；默认随机端口")
    parser.add_argument("--listen-host", default="127.0.0.1", help="监听地址；默认仅本机")
    parser.add_argument("--serve", action="store_true", help="长期运行设置页面；公网使用时须设置密码")
    parser.add_argument("--prefill", type=Path, help="从本机 JSON 文件预填设置")
    args = parser.parse_args()
    os.umask(0o077)
    if args.set_admin_password:
        password = getpass.getpass("设置管理密码：")
        if password != getpass.getpass("再输入一次："):
            raise ValueError("两次密码不一致")
        save_admin_password(password)
        print("管理账号 admin 的密码已设置。")
        return
    if args.listen_host != "127.0.0.1" and not args.serve:
        raise ValueError("非本机监听需要 --serve")
    STATE.serve = args.serve
    if args.serve and admin_mode() != "password":
        raise ValueError("公网设置页需要密码；先运行 python setup_web.py --set-admin-password")
    if args.serve:
        sys.modules.setdefault("setup_web", sys.modules[__name__])
        from dashboard import serve
        serve(args.listen_host, args.port)
        return
    STATE.prefill = prefill_from_config()
    if args.prefill:
        STATE.prefill = json.loads(args.prefill.read_text(encoding="utf-8"))
    server = ThreadingHTTPServer((args.listen_host, args.port), Handler)
    url = f"http://{args.listen_host}:{server.server_address[1]}/"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"设置页面：{url}")
    if not args.no_open:
        webbrowser.open(url)
    try:
        with sync_playwright() as playwright:
            if args.reuse_auth and not (ROOT / "auth.json").exists():
                raise FileNotFoundError("缺少 auth.json，无法复用登录状态")
            browser = playwright.chromium.launch(headless=args.reuse_auth)
            try:
                context = browser.new_context(
                    locale="zh-CN",
                    **({"storage_state": str(ROOT / "auth.json")} if args.reuse_auth else {}),
                )
                page = context.new_page()
                page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=30000)
                if not args.reuse_auth:
                    print("请在抖音窗口扫码登录。")
                wait_for_chat_login(page, 300000)
                STATE.friends = collect_chat_names(page)
                for name in STATE.prefill.get("friends", []):
                    if name not in STATE.friends:
                        STATE.friends.append(name)
                if STATE.friends:
                    try:
                        open_friend(page, STATE.friends[0])
                        STATE.stickers = sticker_names(page)
                    except Exception:
                        pass
                context.storage_state(path=str(ROOT / "auth.json"))
                (ROOT / "auth.json").chmod(0o600)
                STATE.ready = True
                STATE.updated_at = time.monotonic()
                print("登录成功，请回到设置页面选择好友、时间和消息。")
            finally:
                browser.close()
        if args.serve:
            thread.join()
        else:
            STATE.done.wait()
            print("设置已保存。下一步运行 python spark.py --dry-run。")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
