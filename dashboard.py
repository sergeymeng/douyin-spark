"""Protected server dashboard for account setup and login."""

from __future__ import annotations

import html
import json
import logging
import os
import secrets
import re
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from playwright.sync_api import sync_playwright

import setup_web
from accounts import member_login, register
from accounts import MAIN, add_account, beijing_now, get_account, list_accounts, paths, update_account
from configure import collect_chat_names, write_private_json
from reports import LABEL, Report, read_json
from notify import configure_channel, configured_url
from spark import (CHAT_URL, CHALLENGES, LOGIN_PROMPTS, SEARCH, SparkError,
                   any_visible, load_chat, open_friend, sticker_names, validate_config,
                   wait_for_chat_login)


LOGGER = logging.getLogger("spark.dashboard")
JOBS: dict[str, "AccountJob"] = {}
JOBS_LOCK = threading.Lock()
CACHE: dict[str, tuple[list[str], list[str]]] = {}


class AccountJob:
    def __init__(self, account_id: str, mode: str) -> None:
        self.id = secrets.token_urlsafe(8)
        self.account_id = account_id
        self.mode = mode
        self.state = "checking" if mode == "check" else "scanning"
        self.message = "正在检查…" if mode == "check" else "正在打开抖音扫码页…"
        self.qr: bytes | None = None
        self.qr_source = ""
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def set_state(self, state: str, message: str) -> None:
        with self.lock:
            self.state, self.message = state, message

    def info(self) -> dict:
        with self.lock:
            return {"id": self.id, "state": self.state, "message": self.message,
                    "qr": self.qr is not None}

    def run(self) -> None:
        _config, auth_path, _directory = paths(self.account_id)
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    context = browser.new_context(
                        locale="zh-CN", viewport={"width": 1280, "height": 800},
                        **({"storage_state": str(auth_path)}
                           if self.mode == "check" and auth_path.exists() else {}),
                    )
                    page = context.new_page()
                    if self.mode == "check":
                        if not auth_path.exists():
                            self.set_state("expired", "还没登录，点“重新登录”扫码。")
                            return
                        try:
                            load_chat(page)
                        except SparkError:
                            if any_visible(page, LOGIN_PROMPTS + CHALLENGES):
                                self.set_state("expired", "登录已失效，点“重新登录”扫码。")
                            else:
                                self.set_state("error", "聊天页没加载出来，稍后再检查一次。")
                            return
                    else:
                        page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=30000)
                        deadline = time.monotonic() + 300
                        next_reload = time.monotonic() + 90
                        while time.monotonic() < deadline:
                            if any_visible(page, SEARCH) and not any_visible(page, LOGIN_PROMPTS + CHALLENGES):
                                try:
                                    wait_for_chat_login(page, 10000)
                                    break
                                except SparkError:
                                    pass
                            image = page.locator('img[src^="data:image/png;base64,"]').first
                            if image.count() and image.is_visible():
                                source = image.get_attribute("src") or ""
                                if source != self.qr_source:
                                    screenshot = image.screenshot(timeout=5000)
                                    with self.lock:
                                        self.qr = screenshot
                                        self.qr_source = source
                                        self.message = "用抖音扫一扫，扫码后在手机上确认。"
                            if time.monotonic() >= next_reload:
                                page.reload(wait_until="domcontentloaded", timeout=30000)
                                next_reload = time.monotonic() + 90
                            page.wait_for_timeout(1500)
                        else:
                            self.set_state("error", "二维码已超时，点“重新登录”再试。")
                            return
                    auth_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with tempfile.NamedTemporaryFile(dir=auth_path.parent, prefix="auth-",
                                                     suffix=".json", delete=False) as file:
                        temp = Path(file.name)
                    try:
                        context.storage_state(path=str(temp))
                        temp.chmod(0o600)
                        os.replace(temp, auth_path)
                    finally:
                        temp.unlink(missing_ok=True)
                    self.set_state("loading", "已登录，正在更新好友列表…")
                    try:
                        friends = collect_chat_names(page)
                        stickers: list[str] = []
                        if friends:
                            open_friend(page, friends[0])
                            stickers = sticker_names(page)
                        CACHE[self.account_id] = (friends, stickers)
                        self.set_state("ready", "已登录，好友和表情已更新。")
                    except Exception:
                        LOGGER.exception("登录成功，更新聊天列表失败：%s", self.account_id)
                        self.set_state("ready", "已登录；好友列表暂时没更新，可再点“检查登录”。")
                finally:
                    browser.close()
        except Exception:
            LOGGER.exception("账号检查失败：%s", self.account_id)
            self.set_state("error", "抖音页面没打开，稍后再试。")


def start_job(account_id: str, mode: str) -> AccountJob:
    get_account(account_id)
    if mode not in {"check", "login"}:
        raise ValueError("无效的操作")
    with JOBS_LOCK:
        current = JOBS.get(account_id)
        if current and current.thread.is_alive():
            return current
        if sum(j.thread.is_alive() for j in JOBS.values()) >= 3:
            raise ValueError("扫码服务正在使用中，请稍后再试")
        job = AccountJob(account_id, mode)
        JOBS[account_id] = job
        job.thread.start()
        return job


CSS = setup_web.STYLE + """
.accounts{display:flex;align-items:center;gap:10px;margin:0 0 18px}
.accounts select{height:38px;min-width:170px;border:1px solid #d7d7d3;border-radius:6px;background:#fff;padding:0 10px}
.accounts form{border:0;background:none;display:flex;gap:8px;flex:1}
.accounts input{height:38px;min-width:0;max-width:190px;border:1px solid #d7d7d3;border-radius:6px;padding:0 10px}
.small-button{border:1px solid #d7d7d3;background:#fff;border-radius:6px;padding:8px 12px;color:#333}
.small-button:hover{background:#f3f3f1}.status-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.status-text{color:#555;font-size:13px}.qr{display:block;width:230px;height:230px;image-rendering:pixelated;margin:18px 0 0}
.account-card{background:#fff;border:1px solid #dededb;border-radius:10px;padding:22px 28px;margin-bottom:16px}
.account-card h2{margin:0 0 12px}.account-card p{margin:9px 0 0;color:#777;font-size:12px}
.inline-form{border:0;background:none;display:inline}.inline-form button{margin:0}
.notification-form{margin-top:16px}.notification-form section{border-bottom:0}
@media(max-width:600px){.accounts{align-items:stretch;flex-direction:column}.accounts form{width:100%}.accounts input{flex:1}.account-card{padding:18px 16px}}
"""


def _prefill(config: dict) -> dict:
    message = config["message"]
    value = message["value"]
    mode = ("fire" if value == "续火花" else "sticker") if message["type"] == "sticker" else ("emoji" if value in setup_web.EMOJI else "text")
    return {"friends": config["friends"], "send_time": config["send_time"],
            "mode": mode, "text": value if mode == "text" else "",
            "emoji": value if mode == "emoji" else "🔥",
            "sticker": value if mode == "sticker" else ""}


def render_reports(directory: Path, report_id: str) -> str:
    if report_id:
        if not re.fullmatch("[a-f0-9]{32}", report_id):
            raise ValueError("任务记录不存在")
        selected = read_json(directory / "reports" / (report_id + ".json"))
        if not selected:
            raise ValueError("任务记录不存在")
        records = [selected]
    else:
        records = [read_json(directory / f"report-{kind}.json") for kind in ("daily", "check")]
    cards = []
    for data in filter(None, records):
        report = Report(**data)
        if report.kind == "daily":
            title, _, _ = report.summary()
        else:
            passed = sum(v == "checked" for v in report.results.values())
            title = f"发送前检查 · {passed}/{len(report.results)} 位好友通过"
        rows = "".join(f"<li>{html.escape(friend)} · {LABEL[code]}</li>"
                       for friend, code in report.results.items())
        issue = LABEL.get(report.problem, "") or LABEL.get(report.access, "")
        cards.append(f'<section class="account-card"><h2>{html.escape(title)}</h2>'
                     f'<p>{html.escape(report.started[:16].replace("T", " "))} 北京时间 {issue}</p>'
                     f'<details {"open" if report_id else ""}><summary>逐人结果</summary><ul>{rows}</ul></details></section>')
    return '<div id="run-report">' + "".join(cards) + '</div>' if cards else ""


def render_dashboard(account_id: str, notice: str = "", *, csrf: str, admin: bool, report_id: str = "", notification_notice: bool = False) -> str:
    account = get_account(account_id)
    config_path, auth_path, _directory = paths(account_id)
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else None
    prefill = _prefill(config) if config else {}
    cached_friends, stickers = CACHE.get(account_id, ([], []))
    friends = list(dict.fromkeys(cached_friends + prefill.get("friends", [])))
    selected = set(prefill.get("friends", []))
    option_html = "".join(f'<option value="{a["id"]}" {"selected" if a["id"] == account_id else ""}>{html.escape(a["name"])}</option>' for a in list_accounts() if "credentials" not in a) if admin else ""
    friend_html = "".join(
        f'<label class="friend" data-name="{html.escape(name, quote=True)}"><input type="checkbox" name="friend" value="{html.escape(name, quote=True)}" {"checked" if name in selected else ""}><span title="{html.escape(name, quote=True)}">{html.escape(name)}</span></label>'
        for name in friends
    ) or '<div class="empty">登录后会显示聊天列表，也可以在下面填写准确备注名。</div>'
    mode = prefill.get("mode", "fire")
    emoji_options = "".join(f'<option value="{html.escape(x, quote=True)}" {"selected" if x == prefill.get("emoji") else ""}>{x}</option>' for x in setup_web.EMOJI)
    sticker_choices = list(dict.fromkeys(([prefill["sticker"]] if prefill.get("sticker") else []) + stickers))
    sticker_options = "".join(f'<option value="{html.escape(x, quote=True)}" {"selected" if x == prefill.get("sticker") else ""}>{html.escape(x)}</option>' for x in sticker_choices)
    job = JOBS.get(account_id)
    status = job.info()["message"] if job else ("登录状态尚未检查。" if auth_path.exists() else "还没登录。")
    pause = "" if account_id == MAIN else f'''<form class="inline-form" method="post" action="toggle"><input type="hidden" name="token" value="{csrf}"><input type="hidden" name="account" value="{account_id}"><button class="small-button" type="submit">{"暂停定时" if account["enabled"] else "启用定时"}</button></form>'''
    picker = f'''<div class="accounts"><select id="account-picker" aria-label="选择抖音账号">{option_html}</select><form method="post" action="add-account"><input type="hidden" name="token" value="{csrf}"><input name="name" maxlength="40" placeholder="新账号名称" required><button class="small-button" type="submit">添加账号</button></form></div>''' if admin else ""
    topic_file = None if admin else _directory / "notify_url"
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>定时发送</title><style>{CSS}</style></head><body><main class="page">
    <header><h1>定时发送</h1><form class="logout" method="post" action="logout"><button type="submit">退出</button></form></header>
    {picker}
    {f'<p class="notice">{html.escape(notice)}</p>' if notice and not notification_notice else ''}
    <div class="account-card"><h2>{html.escape(account["name"])}</h2><div class="status-row"><span class="status-text" id="login-status">{html.escape(status)}</span><button class="small-button" type="button" data-job="check">检查登录</button><button class="small-button" type="button" data-job="login">{"重新登录" if auth_path.exists() else "扫码绑定抖音号"}</button>{pause}</div><img class="qr" id="login-qr" alt="抖音登录二维码" hidden><p>仅绑定你本人的抖音号。扫码确认后，本站会在服务器保存网页登录状态，用于读取好友列表和按你的设置发送私信。</p></div>
    {render_reports(_directory, report_id)}
    <form method="post" action="save"><input type="hidden" name="token" value="{csrf}"><input type="hidden" name="account" value="{account_id}">
      <section><div class="section-head"><h2>好友</h2><span class="count" id="friend-count"></span></div><input class="search" id="friend-search" type="search" placeholder="搜索昵称" aria-label="搜索好友"><div class="friend-list">{friend_html}</div><label class="field">列表里没有？输入准确备注名<input name="other_friends" placeholder="多个名字用逗号隔开"></label></section>
      <section><div class="section-head"><h2>时间</h2><span class="hint">每天</span></div><div class="when"><input class="time" name="send_time" value="{html.escape(prefill.get("send_time", "20:30"), quote=True)}" pattern="([01][0-9]|2[0-3]):[0-5][0-9]" placeholder="HH:MM" required><span>北京时间</span></div></section>
      <section><div class="section-head"><h2>内容</h2></div><div class="modes">
        <label class="mode"><input type="radio" name="mode" value="fire" {"checked" if mode == "fire" else ""}>续火花表情</label>
        <label class="mode"><input type="radio" name="mode" value="text" {"checked" if mode == "text" else ""}>文字</label>
        <label class="mode"><input type="radio" name="mode" value="emoji" {"checked" if mode == "emoji" else ""}>普通表情</label>
        <label class="mode"><input type="radio" name="mode" value="sticker" {"checked" if mode == "sticker" else ""}>其他抖音表情</label></div>
        <label class="field" data-for="text">发送文字<input name="text" maxlength="200" value="{html.escape(prefill.get("text", "今天怎么样？"), quote=True)}"></label>
        <label class="field" data-for="emoji">选择表情<select name="emoji">{emoji_options}</select></label>
        <label class="field" data-for="sticker">选择抖音表情<select name="sticker">{sticker_options}</select></label></section>
      <footer><button class="save" type="submit">保存设置</button><span class="hint">保存后不会立即发送</span></footer></form>
    <form id="notifications" class="notification-form" method="post" action="notifications"><input type="hidden" name="token" value="{csrf}"><input type="hidden" name="account" value="{account_id}"><section>
      {f'<p class="notice" role="status">{html.escape(notice)}</p>' if notification_notice and notice else ''}
      {setup_web.notification_fields(configured_url(topic_file))}
    </section></form>
    </main><script>
    const account = {json.dumps(account_id)};
    const token = {json.dumps(csrf)};
    document.querySelector('#account-picker')?.addEventListener('change', e => location.href = '?a=' + encodeURIComponent(e.target.value));
    document.querySelector('#friend-search').addEventListener('input', e => {{const q=e.target.value.toLowerCase();document.querySelectorAll('.friend').forEach(x=>x.hidden=!x.dataset.name.toLowerCase().includes(q));}});
    function update() {{const count=document.querySelectorAll('input[name="friend"]:checked').length;document.querySelector('#friend-count').textContent='已选 '+count+' 人';const mode=document.querySelector('input[name="mode"]:checked')?.value;document.querySelectorAll('[data-for]').forEach(x=>x.hidden=x.dataset.for!==mode);}}
    document.querySelector('form[action="save"]').addEventListener('change',update);update();
    document.querySelectorAll('[data-job]').forEach(button=>button.addEventListener('click',async()=>{{button.disabled=true;const body=new URLSearchParams({{account,token,mode:button.dataset.job}});try{{const r=await fetch('job',{{method:'POST',body}});const data=await r.json();if(!r.ok)throw Error(data.error);document.querySelector('#login-status').textContent=data.message;}}catch(e){{document.querySelector('#login-status').textContent=e.message;}}finally{{button.disabled=false;}}}}));
    async function poll(){{try{{const r=await fetch('api/status?a='+encodeURIComponent(account),{{cache:'no-store'}});if(!r.ok)return;const data=await r.json();if(!data.id)return;document.querySelector('#login-status').textContent=data.message;const image=document.querySelector('#login-qr');image.hidden=!(data.state==='scanning'&&data.qr);if(!image.hidden)image.src='api/qr?a='+encodeURIComponent(account)+'&t='+Date.now();if(data.state==='ready'&&sessionStorage.getItem('spark-job-'+account)!==data.id){{sessionStorage.setItem('spark-job-'+account,data.id);location.reload();}}}}catch(e){{}}}}
    setInterval(poll,2500);poll();
    </script></body></html>'''


def registration_page(error: str = "") -> str:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1"><title>注册 · 定时发送</title>
    <style>{setup_web.STYLE}</style></head><body><main class="login-page"><h1>注册定时发送账号</h1>
    <p>注册后，用抖音扫码绑定自己的账号，即可设置每天给哪些好友发送消息。</p>
    <form class="login" method="post" action="register">
    <label>用户名<input name="username" autocomplete="username" required autofocus minlength="3" maxlength="32"
    pattern="[A-Za-z0-9_]{{3,32}}" placeholder="3–32 位字母、数字或下划线"></label>
    <label>密码<input name="password" type="password" autocomplete="new-password" minlength="8" maxlength="128" required></label>
    <label>确认密码<input name="confirm" type="password" autocomplete="new-password" required></label>
    {f'<p class="error">{html.escape(error)}</p>' if error else ''}
    <button class="save" type="submit">注册并登录</button></form><p><a href="login">已有账号，去登录</a></p></main></body></html>"""


SIGNUP_ATTEMPTS: dict[str, list[float]] = {}
SIGNUP_LOCK = threading.Lock()
AUTH_SLOTS = threading.BoundedSemaphore(3)


class Handler(setup_web.Handler):
    def login_query(self) -> str:
        fields = parse_qs(urlparse(self.path).query)
        report = fields.get("report", [""])[0]
        if not re.fullmatch("[a-f0-9]{32}", report):
            return ""
        query = {"report": report}
        account = fields.get("a", [""])[0]
        if re.fullmatch("[a-f0-9]{12}|main", account):
            query["a"] = account
        return "?" + urlencode(query)


    def login_owner(self, username: str, password: str) -> str | None:
        if username == "admin":
            return super().login_owner(username, password)
        return member_login(username, password)

    def signup(self) -> None:
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096:
                raise ValueError("注册信息长度无效")
            address = self.client_ip()
            now = time.monotonic()
            with SIGNUP_LOCK:
                for key in list(SIGNUP_ATTEMPTS):
                    if SIGNUP_ATTEMPTS[key][-1] <= now - 600:
                        del SIGNUP_ATTEMPTS[key]
                attempts = [t for t in SIGNUP_ATTEMPTS.get(address, []) if t > now - 600]
                limited = len(attempts) >= 5
                if not limited:
                    SIGNUP_ATTEMPTS[address] = attempts + [now]
            if limited:
                self.respond(registration_page("注册尝试过多，请稍后再试。"), 429)
                return
            form = parse_qs(self.rfile.read(size).decode("utf-8"))
            password = form.get("password", [""])[0]
            if password != form.get("confirm", [""])[0]:
                raise ValueError("两次密码不一致")
            account = register(form.get("username", [""])[0], password)
        except ValueError as exc:
            self.respond(registration_page(str(exc)), 400)
            return
        self.issue_session(account["id"])

    def authorized_account(self, owner: str, requested: str | None) -> str:
        account_id = requested if requested is not None else (MAIN if owner == "admin" else owner)
        if owner != "admin" and account_id != owner:
            raise ValueError("账号不存在")
        account = get_account(account_id)
        if owner == "admin" and "credentials" in account:
            raise ValueError("账号不存在")
        return account_id

    def _form(self, csrf: str) -> dict[str, list[str]]:
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 <= size <= 10000:
            raise ValueError("表单太大")
        form = parse_qs(self.rfile.read(size).decode("utf-8"), keep_blank_values=True)
        if form.get("token", [""])[0] != csrf:
            raise PermissionError("页面已过期，请刷新后重试")
        return form

    def _back(self, account_id: str, message: str = "") -> None:
        query = {"a": account_id}
        if message:
            query["notice"] = message
        self.redirect("./?" + urlencode(query))

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/register":
            if self.authenticated():
                self.redirect("./")
            else:
                self.respond(registration_page())
            return
        if url.path == "/login":
            return super().do_GET()
        session = self.session()
        if not session:
            if url.path.startswith("/api/"):
                self.respond_json({"error": "请先登录"}, status=401)
            else:
                self.redirect("login" + self.login_query())
            return
        try:
            account_id = self.authorized_account(session.owner, parse_qs(url.query).get("a", [None])[0])
            if url.path == "/":
                notice = parse_qs(url.query).get("notice", [""])[0]
                self.respond(render_dashboard(account_id, notice, csrf=session.csrf, admin=session.owner == "admin",
                                             report_id=parse_qs(url.query).get("report", [""])[0],
                                             notification_notice="notifications" in parse_qs(url.query)))
            elif url.path == "/api/status":
                job = JOBS.get(account_id)
                self.respond_json(job.info() if job else {"state": "unknown"})
            elif url.path == "/api/qr":
                job = JOBS.get(account_id)
                with job.lock if job else threading.Lock():
                    image = job.qr if job else None
                if not image:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(image)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(image)
            else:
                self.send_error(404)
        except ValueError as exc:
            self.respond(html.escape(str(exc)), status=404)

    def do_POST(self) -> None:
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.send_error(403)
            return
        if urlparse(self.path).path in {"/login", "/register"}:
            if setup_web.admin_mode() != "password":
                self.send_error(503)
                return
            if not AUTH_SLOTS.acquire(blocking=False):
                self.send_error(429)
                return
            try:
                if urlparse(self.path).path == "/register":
                    self.signup()
                else:
                    self.login()
            finally:
                AUTH_SLOTS.release()
            return
        if self.path == "/logout":
            return super().do_POST()
        session = self.session()
        if not session:
            self.send_error(401)
            return
        try:
            form = self._form(session.csrf)
            account_id = self.authorized_account(session.owner, form.get("account", [None])[0])
            if self.path == "/add-account":
                if session.owner != "admin":
                    raise PermissionError("此账号已分配独立的抖音绑定位置")
                account = add_account(form.get("name", [""])[0])
                self._back(account["id"], "账号已添加，先扫码登录。")
            elif self.path == "/job":
                job = start_job(account_id, form.get("mode", [""])[0])
                self.respond_json(job.info())
            elif self.path == "/toggle":
                if account_id == MAIN:
                    raise ValueError("原账号的定时任务由服务器服务管理")
                account = get_account(account_id)
                enabled = not account["enabled"]
                config_path, auth_path, _directory = paths(account_id)
                if enabled and (not config_path.exists() or not auth_path.exists()):
                    raise ValueError("先登录并保存设置")
                update_account(account_id, enabled=enabled,
                               activated_at=beijing_now().isoformat() if enabled else account["activated_at"])
                self._back(account_id, "定时发送已启用。" if enabled else "定时发送已暂停。")
            elif self.path == "/save":
                self.save_account(account_id, form)
            elif self.path == "/notifications":
                self.save_notifications(account_id, form)
            else:
                self.send_error(404)
        except PermissionError as exc:
            self.respond(html.escape(str(exc)), status=403)
        except (ValueError, SparkError) as exc:
            self.respond(html.escape(str(exc)), status=400)

    def save_account(self, account_id: str, form: dict[str, list[str]]) -> None:
        config_path, auth_path, _directory = paths(account_id)
        if not auth_path.exists():
            raise ValueError("先登录抖音账号")
        friends = list(dict.fromkeys(form.get("friend", []) + [
            x.strip() for x in form.get("other_friends", [""])[0].replace("，", ",").split(",") if x.strip()
        ]))
        mode = form.get("mode", [""])[0]
        if mode == "fire":
            message = {"type": "sticker", "value": "续火花"}
        elif mode == "sticker":
            value = form.get("sticker", [""])[0]
            previous = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
            if value not in CACHE.get(account_id, ([], []))[1] and previous.get("message") != {"type": "sticker", "value": value}:
                raise ValueError("先检查登录，更新可选表情")
            message = {"type": "sticker", "value": value}
        elif mode == "emoji":
            value = form.get("emoji", [""])[0]
            if value not in setup_web.EMOJI:
                raise ValueError("请选择普通表情")
            message = {"type": "text", "value": value}
        elif mode == "text":
            message = {"type": "text", "value": form.get("text", [""])[0].strip()}
        else:
            raise ValueError("请选择发送内容")
        config = validate_config({"timezone": "Asia/Shanghai", "send_time": form.get("send_time", [""])[0],
                                  "friends": friends, "message": message})
        config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        old_config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else None
        write_private_json(config_path, config)
        if account_id == MAIN:
            if not old_config or old_config["send_time"] != config["send_time"]:
                try:
                    setup_web.wait_for_installed_timer(config["send_time"], config_path.stat().st_mtime_ns)
                except Exception:
                    if old_config:
                        write_private_json(config_path, old_config)
                    else:
                        config_path.unlink(missing_ok=True)
                    raise ValueError("定时器没有更新，设置未保存")
        elif old_config is None:
            update_account(account_id, enabled=True, activated_at=beijing_now().isoformat())
        self._back(account_id, "已保存，下次按这个时间发送。")

    def save_notifications(self, account_id: str, form: dict[str, list[str]]) -> None:
        directory = paths(account_id)[2] if "credentials" in get_account(account_id) else setup_web.ROOT / "data"
        path = directory / "notify_url"
        message = configure_channel(path, form.get("action", ["test"])[0], form.get("notify_url", [""])[0])
        query = urlencode({"a": account_id, "notifications": "1", "notice": message})
        self.redirect("./?" + query + "#notifications")


def serve(host: str, port: int) -> None:
    setup_web.STATE.serve = True
    if setup_web.admin_mode() != "password":
        raise ValueError("服务器管理页需要管理密码")
    server = ThreadingHTTPServer((host, port), Handler)
    LOGGER.info("管理页监听 %s:%d", host, server.server_address[1])
    try:
        server.serve_forever()
    finally:
        server.server_close()
