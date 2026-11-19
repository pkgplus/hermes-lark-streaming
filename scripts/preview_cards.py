#!/usr/bin/env python3
"""Render the real menu cards with a fake host and write preview/cards.html.

Dev aid only: it imports the plugin's BotMenu, captures the exact card JSON that
would be sent to Feishu, then draws an approximation of the Feishu card renderer
so the layout can be reviewed without sending messages.
"""

from __future__ import annotations

import asyncio
import html
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, "/home/pi5/.hermes/hermes-agent")

from hermes_lark_streaming.bot_menu import BotMenu, Owner  # noqa: E402

CAPTURED: list[dict] = []


class FakeClient:
    async def send_card_to_chat(self, chat_id, card):
        CAPTURED.append(card)
        return "om_card"

    async def update_card_message(self, message, card):
        CAPTURED.append(card)


class FakeAdapter:
    def __init__(self):
        self.cards = SimpleNamespace(_client=FakeClient())
        self._is_sender_authorized = lambda *a, **k: True
        self._admit_synthetic = lambda *a, **k: None

    def get_session_model_settings(self, source):
        return {"model": "gpt-6.1-sol", "provider": "mytokens-resp", "session_key": "lane"}

    def list_session_model_picker_providers(self, source, max_models=None, include_moa=False):
        return [
            {"slug": "mytokens-comp", "name": "comp", "models": ["deepseek-v4-pro", "glm-5.2", "qwen3.7-max"]},
            {"slug": "mytokens-resp", "name": "resp", "models": ["gpt-6.2-sol", "gpt-6.1-sol", "gpt-5.6-terra", "gpt-5.5"]},
            {"slug": "mytokens-msg", "name": "msg", "models": ["claude-opus-5.5", "claude-sonnet-5.5"]},
        ]


class FakeHost:
    async def current(self, source):
        return "20261119_182700_b1871fd8"

    async def sessions(self, source, page):
        import time

        now = time.time()
        rows = [
            {"id": "20261119_182700_b1871fd8", "title": "机器人自定义菜单打开飞书小程序", "last_active": now - 40, "message_count": 290},
            {"id": "20261119_130129_75452863", "title": "飞书机器人菜单增加模型切换、会话切换与消息查看", "last_active": now - 3 * 3600, "message_count": 358},
            {"id": "20261119_084958_be4ad4", "title": "开发飞书 Hermes 菜单加号，实现模型与会话切换及历史查看", "last_active": now - 86400 - 3600, "message_count": 227},
        ]
        return rows, False

    async def history(self, source, sid, page):
        return [
            ("user", "在现有飞书插件基础上，可以实现机器人自定义菜单增加模型切换、会话切换吗？"),
            ("assistant", "可以。沿用现有长连接与原生会话切换能力，新增菜单事件接入与交互卡片，保留原命令权限与费用确认。"),
        ], False


TEMPLATES = {
    "blue": "#3370FF",
    "wathet": "#4E83FD",
    "orange": "#FF8800",
    "grey": "#8F959E",
    "green": "#34C724",
    "red": "#F54A45",
}


def inline(text: str) -> str:
    out = html.escape(text, quote=False)
    # **bold**
    parts = out.split("**")
    out = "".join(f"<strong>{p}</strong>" if i % 2 else p for i, p in enumerate(parts))
    # <font color='x'>…</font>
    import re

    out = re.sub(
        r"&lt;font color=['\"](\w+)['\"]&gt;(.*?)&lt;/font&gt;",
        lambda m: f'<span style="color:{color(m.group(1))}">{m.group(2)}</span>',
        out,
        flags=re.S,
    )
    return out.replace("\n", "<br>")


def color(name: str) -> str:
    return {"grey": "#8F959E", "green": "#34C724", "red": "#F54A45", "blue": "#3370FF"}.get(name, "#1F2329")


def render_element(el: dict) -> str:
    tag = el.get("tag")
    if tag == "div":
        text = el["text"]
        cls = "div plain" if text["tag"] == "plain_text" else "div md"
        return f'<div class="{cls}">{inline(text["content"])}</div>'
    if tag == "hr":
        return '<div class="hr"></div>'
    if tag == "note":
        return '<div class="note">' + "".join(inline(e["content"]) for e in el["elements"]) + "</div>"
    if tag == "action":
        return '<div class="actions">' + "".join(render_element(b) for b in el["actions"]) + "</div>"
    if tag == "button":
        style = el.get("type", "default")
        size = el.get("size", "small")
        return f'<span class="btn {style} {size}">{html.escape(el["text"]["content"])}</span>'
    if tag == "column_set":
        cols = []
        for col in el["columns"]:
            width = "flex:1" if col.get("width") == "weighted" else "flex:0 0 auto"
            cols.append(f'<div class="col" style="{width}">' + "".join(render_element(e) for e in col["elements"]) + "</div>")
        bg = " grey" if el.get("background_style") == "grey" else ""
        return f'<div class="cols{bg}">' + "".join(cols) + "</div>"
    return ""


def render_card(card: dict) -> str:
    header = card.get("header") or {}
    tmpl = header.get("template", "blue")
    bar = TEMPLATES.get(tmpl, "#3370FF")
    title = html.escape(header.get("title", {}).get("content", ""))
    body = "".join(render_element(e) for e in card.get("elements", []))
    return f'''<section class="card">
  <div class="head" style="background:{bar}"><span>{title}</span></div>
  <div class="body">{body}</div>
</section>'''


async def main() -> None:
    adapter = FakeAdapter()
    menu = BotMenu(adapter)
    menu.host = FakeHost()
    owner = Owner(
        ("ou_owner", "tenant", "on_union"),
        SimpleNamespace(chat_id="oc_dm", chat_type="dm", user_id="tenant", user_id_alt="on_union"),
        10**12,
    )
    current = "20261119_182700_b1871fd8"
    await menu.render(owner, current, ("model", 0), "")
    await menu.render(owner, current, ("session", 0), "")
    await menu.render(owner, current, ("history", "20261119_130129_75452863", 0), "")
    await menu.render(owner, current, ("select_model", "mytokens-resp", "gpt-6.2-sol"), "")

    labels = ["模型选择", "会话列表", "消息预览", "切换确认"]
    cards = "".join(
        f'<div class="frame"><div class="label">{labels[i]}</div>{render_card(card)}</div>'
        for i, card in enumerate(CAPTURED[:4])
    )

    doc = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>Hermes 菜单卡片 · 预览</title>
<style>
:root{{--t1:#1f2329;--t2:#646a73;--t3:#8f959e;--line:#dee0e3;--soft:#eff0f1;}}
*{{box-sizing:border-box}}
body{{margin:0;background:#f0f1f2;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;
     display:flex;gap:24px;padding:40px;align-items:flex-start}}
.frame{{width:520px}}
.label{{font-size:12px;color:#646a73;margin:0 0 8px 4px;letter-spacing:.3px}}
.card{{background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(31,35,41,.10),0 0 0 1px rgba(31,35,41,.04)}}
.head{{padding:12px 16px;color:#fff;font-size:16px;font-weight:600}}
.body{{padding:12px 16px 16px}}
.div{{font-size:14px;line-height:22px;color:var(--t1);margin:6px 0}}
.div.plain{{color:#3f444b}}
.hr{{height:1px;background:var(--soft);margin:10px 0}}
.note{{font-size:12px;color:var(--t3);margin-top:10px}}
.actions{{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;align-items:center}}
.btn{{display:inline-flex;align-items:center;justify-content:center;height:28px;padding:0 12px;border-radius:6px;
     border:1px solid var(--line);font-size:13px;color:var(--t1);background:#fff}}
.btn.medium{{height:34px;padding:0 20px;font-size:14px}}
.btn.primary{{background:#3370ff;border-color:#3370ff;color:#fff}}
.cols{{display:flex;align-items:center;gap:12px;padding:6px 0}}
.cols.grey{{background:#f5f6f7;border-radius:6px;padding:8px 10px;margin:6px 0}}
.col{{min-width:0}}
</style></head><body>{cards}</body></html>'''
    out = ROOT / "preview" / "cards.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    (ROOT / "preview" / "cards.json").write_text(json.dumps(CAPTURED[:4], ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote", out, "cards:", len(CAPTURED))


if __name__ == "__main__":
    asyncio.run(main())
