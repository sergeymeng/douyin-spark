import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from spark import (SparkError, _editor_text, open_friend, outgoing_count,
                   send_text, sticker_message_ids, verify_after_reload)


HTML = """
<input placeholder="搜索好友" />
<div class="SearchPanelitembox">
  <span class="SearchPanelitemtitle">小林同学</span>
  <button class="SearchPanelitemchat_btn" onclick="openChat('小林同学')">发消息</button>
</div>
<div class="SearchPanelitembox">
  <span class="SearchPanelitemtitle">小林</span>
  <button class="SearchPanelitemchat_btn" onclick="openChat('小林')">发消息</button>
</div>
<div class="RightPanelHeader"><span class="title"></span></div>
<div class="DraftEditor-editor"><div contenteditable="true"></div></div>
<script>
function openChat(name) { document.querySelector('.RightPanelHeader .title').textContent = name; }
</script>
"""

GROUP_HTML = """
<input placeholder="搜索好友" />
<div class="SearchPanelitembox">
  <div class="SearchPanelitemtitle"><span class="SearchPanelitemsearch_highlight">大连三鲨</span><span class="SearchPanelitemsuffix">(4)</span></div>
  <button class="SearchPanelitemchat_btn" onclick="document.querySelector('.RightPanelHeadertitle').textContent='大连三鲨(4)'">发消息</button>
</div>
<div class="RightPanelHeader"><div class="RightPanelHeadertitle"></div></div>
<div class="DraftEditor-editor"><div contenteditable="true"></div></div>
"""


class BrowserTests(unittest.TestCase):
    def test_sticker_verified_when_visible_message_count_stays_the_same(self):
        def bubble(message_id):
            return ("<div class='messageMessageBoxmessageBox'>"
                    "<span class='messageMessageBoxisFromMe'></span>"
                    "<img class='MessageItemEmojiimage' "
                    f"src='https://example.com/fire.png?l={message_id}'>"
                    "</div>")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(bubble("older-message"))
                message = {"type": "sticker", "value": "续火花"}
                before_count = outgoing_count(page, message)
                before_ids = sticker_message_ids(page)
                page.route("**/chat", lambda route: route.fulfill(
                    body=bubble("new-message"), content_type="text/html"))
                with patch("spark.wait_for_chat_login"), patch("spark.open_friend"):
                    verify_after_reload(page, "小林", message, before_count, before_ids)
                self.assertEqual(before_count, outgoing_count(page, message))
                self.assertEqual({"new-message"}, sticker_message_ids(page))
            finally:
                browser.close()

    def test_exact_friend_selected_over_similar_name(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(HTML)
                open_friend(page, "小林")
                self.assertEqual("小林", page.locator(".RightPanelHeader .title").inner_text())
            finally:
                browser.close()

    def test_unknown_friend_stops(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(HTML)
                with self.assertRaises(SparkError):
                    open_friend(page, "不存在")
            finally:
                browser.close()

    def test_group_member_count_does_not_change_friend_name(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(GROUP_HTML)
                open_friend(page, "大连三鲨")
                self.assertEqual("大连三鲨(4)", page.locator(".RightPanelHeadertitle").inner_text())
            finally:
                browser.close()

    def test_zero_width_editor_placeholder_is_empty(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content('<div contenteditable="true">\u200b</div>')
                self.assertEqual("", _editor_text(page.locator('[contenteditable="true"]')))
            finally:
                browser.close()

    def test_text_send_with_zero_width_placeholder(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content("""
                  <div class="DraftEditor-editor"><div contenteditable="true"><span>\u200b</span></div></div>
                  <button class="messageMsgInputpublishBtn" onclick="send()">发送</button>
                  <div id="messages"></div>
                  <script>
                    function send() {
                      const bubble = document.createElement('div');
                      bubble.className = 'messageMessageBoxmessageBox';
                      bubble.innerHTML = '<span class="messageMessageBoxisFromMe"></span>';
                      bubble.append(document.querySelector('[contenteditable]').innerText);
                      document.querySelector('#messages').append(bubble);
                    }
                  </script>
                """)
                send_text(page, "今天怎么样？")
                self.assertIn("今天怎么样？", page.locator("#messages").inner_text())
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
