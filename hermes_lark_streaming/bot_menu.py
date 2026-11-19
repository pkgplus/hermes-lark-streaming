# ruff: noqa: RUF001
"""Bot custom menu; all authority stays in native ingress and native slash commands."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import shlex
import time
import weakref
from collections import OrderedDict
from copy import copy
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from agent.context_compressor import (  # type: ignore[import-untyped]
    is_compaction_summary_message,
    user_originated_turn_view,
)
from gateway.platforms.event import MessageType

from .menu_host import MenuHost

log = logging.getLogger(__name__)
PAGE_SIZE = 8
TOKEN_TTL = 300
MENU_KEYS = frozenset({"model", "session"})


def field(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def identity(obj: Any) -> tuple[str, str, str]:
    return tuple(str(field(obj, key, "") or "") for key in ("open_id", "user_id", "union_id"))  # type: ignore[return-value]


def relative_time(ts: Any) -> str:
    """Compact Chinese relative time for list rows (empty when unknown)."""
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return ""
    if value > 1e12:  # milliseconds from older stores
        value /= 1000.0
    now = time.time()
    delta = now - value
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    then, today = time.localtime(value), time.localtime(now)
    if (then.tm_year, then.tm_yday) == (today.tm_year, today.tm_yday):
        return time.strftime("今天 %H:%M", then)
    if (then.tm_year, then.tm_yday) == (today.tm_year, today.tm_yday - 1):
        return time.strftime("昨天 %H:%M", then)
    return time.strftime("%m月%d日", then)


def plain_messages(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    result = []
    for row in rows:
        if (
            row.get("role") not in {"user", "assistant"}
            or row.get("tool_calls")
            or row.get("is_summary")
            or row.get("hidden")
        ):
            continue
        # Native content recognition survives metadata-stripping store/wire round-trips.
        # Unwrap human asks before filtering legacy hidden summary wrappers.
        if row.get("role") == "user":
            human_row = user_originated_turn_view(row)
            if human_row is None:
                continue
            row = human_row
        elif is_compaction_summary_message(row):
            continue
        if row.get("display_kind"):
            continue
        content = row.get("content")
        if isinstance(content, list):
            content = "\n".join(
                str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type") == "text"
            )
        if isinstance(content, str) and content.strip():
            result.append((row["role"], content))
    return result


@dataclass
class Owner:
    ids: tuple[str, str, str]
    source: Any
    expires: float


@dataclass
class MenuToken:
    owner: Owner
    message: str
    current: str
    action: tuple[Any, ...]
    expires: float


class MenuDispatcher:
    """Compose two official SDK dispatchers without editing their registries.

    The host exposes a built dispatcher, not a builder hook. HTTP validation is still
    performed by the chosen SDK handler; WS already validates on its single connection.
    """

    def __init__(self, base: Any, menu: Any) -> None:
        self.base, self.menu = base, menu

    def _handler(self, payload: bytes) -> Any:
        try:
            kind = (json.loads(payload).get("header") or {}).get("event_type")
        except (ValueError, TypeError, AttributeError):
            kind = None
        return self.menu if kind == "application.bot.menu_v6" else self.base

    def do(self, req: Any) -> Any:
        # Encrypted HTTP bodies cannot be routed before the SDK decrypts them. The
        # base SDK's decrypt helper is isolated here, never replaced or mutated.
        payload = req.body
        try:
            payload = self.base._decrypt(payload).encode()
        except Exception:
            return self.base.do(req)
        return self._handler(payload).do(req)

    def _do_without_validation(self, payload: bytes) -> Any:
        return self._handler(payload)._do_without_validation(payload)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.base, name)


class BotMenu:
    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter
        self.host = MenuHost(adapter)
        self.owners: OrderedDict[str, Owner] = OrderedDict()
        self.tokens: OrderedDict[str, MenuToken] = OrderedDict()
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        # Session titles seen by the menu, so the preview can name its target.
        self._titles: dict[str, str] = {}

    def observe(self, event: Any) -> None:
        """Only real, normalized inbound DMs establish an app-open-id → DM mapping."""
        raw = field(event.raw_message, "event")
        sender, message = field(raw, "sender"), field(raw, "message")
        ids = identity(field(sender, "sender_id"))
        source = event.source
        if (
            not ids[0]
            or field(sender, "sender_type") != "user"
            or not source
            or source.chat_type != "dm"
            or source.thread_id
            or field(message, "chat_type") != "p2p"
            or field(message, "chat_id") != source.chat_id
            or (ids[1] or ids[0]) != source.user_id
            or (ids[2] or None) != source.user_id_alt
        ):
            return
        if self.adapter._is_sender_authorized(source.user_id, "dm", source.chat_id) is not True:
            return
        self.owners[ids[0]] = Owner(ids, copy(source), time.monotonic() + 86400)
        self.owners.move_to_end(ids[0])
        while len(self.owners) > 1024:
            self.owners.popitem(last=False)

    async def _restore_owner(self, ids: tuple[str, str, str]) -> Owner | None:
        """Rebuild a DM binding lost to a gateway restart; state.db is the durable record."""
        try:
            sources = await self.host.recent_dm_sources()
        except Exception:
            log.warning("Bot menu could not read recent DM sources", exc_info=True)
            return None
        for source in sources:
            if source.user_id != ids[0]:
                continue
            owner = Owner(ids, copy(source), time.monotonic() + 86400)
            if self.authorized(owner, ids):
                self.owners[ids[0]] = owner
                return owner
        return None

    def authorized(self, owner: Owner, ids: tuple[str, str, str]) -> bool:
        # Compare matching tiers only; callback omissions don't invent or swap identities.
        if (
            owner.expires <= time.monotonic()
            or not ids[0]
            or ids[0] != owner.ids[0]
            or any(given and given != saved for given, saved in zip(ids[1:], owner.ids[1:], strict=True))
        ):
            return False
        a = self.adapter
        sender = SimpleNamespace(**dict(zip(("open_id", "user_id", "union_id"), owner.ids, strict=True)))
        return (
            a._admit_synthetic(sender, chat_id=owner.source.chat_id, chat_type="p2p") is None
            and a._is_sender_authorized(owner.source.user_id, "dm", owner.source.chat_id) is True
        )

    async def open(self, data: Any) -> None:
        try:
            event = field(data, "event")
            key = field(event, "event_key")
            if key not in MENU_KEYS:
                return
            ids = identity(field(field(event, "operator"), "operator_id"))
            owner = self.owners.get(ids[0])
            if owner is None:
                owner = await self._restore_owner(ids)
            if owner is None or not self.authorized(owner, ids):
                if owner is None:
                    log.info("Bot menu ignored: no verified DM binding for this user yet")
                return
            lock = self._locks.setdefault(owner.source.chat_id, asyncio.Lock())
            async with lock:
                current = await self.host.current(owner.source)
                if current is None:
                    return
                await self.render(owner, current, (key, 0), "")
        except Exception:
            log.exception("Bot menu unavailable (no session or settings changed)")

    def _prune(self) -> None:
        now = time.monotonic()
        for token in list(self.tokens):
            if self.tokens[token].expires <= now:
                del self.tokens[token]
        while len(self.tokens) > 2048:
            self.tokens.popitem(last=False)

    async def render(self, owner: Owner, current: str, action: tuple[Any, ...], message: str) -> None:
        a, source = self.adapter, owner.source
        kind, *args = action
        generated: dict[str, MenuToken] = {}

        def btn(label: str, target: tuple[Any, ...], style: str = "default", size: str = "small") -> dict[str, Any]:
            token = secrets.token_urlsafe(24)
            generated[token] = MenuToken(owner, message, current, target, time.monotonic() + TOKEN_TTL)
            return {
                "tag": "button",
                "text": {"tag": "plain_text", "content": label[:60]},
                "type": style,
                "size": size,
                "value": {"hermes_menu_token": token},
            }

        def md(content: str) -> dict[str, Any]:
            return {"tag": "div", "text": {"tag": "lark_md", "content": content}}

        def plain(content: str) -> dict[str, Any]:
            return {"tag": "div", "text": {"tag": "plain_text", "content": content}}

        def row(left: list[dict[str, Any]], right: list[dict[str, Any]] | None = None) -> dict[str, Any]:
            cols: list[dict[str, Any]] = [
                {"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "center", "elements": left}
            ]
            if right:
                cols.append({"tag": "column", "width": "auto", "vertical_align": "center", "elements": right})
            return {"tag": "column_set", "flex_mode": "none", "background_style": "default", "columns": cols}

        def note(content: str) -> dict[str, Any]:
            return {"tag": "note", "elements": [{"tag": "plain_text", "content": content}]}

        def actions(*buttons: dict[str, Any]) -> dict[str, Any]:
            return {"tag": "action", "actions": list(buttons)}

        def pager(page: int, target: str, more: bool) -> list[dict[str, Any]]:
            out = []
            if page:
                out.append(btn("上一页", (target, page - 1)))
            if more:
                out.append(btn("下一页", (target, page + 1)))
            return out

        title, template, elements = "Hermes", "blue", []
        if kind == "model":
            settings_fn = getattr(a, "get_session_model_settings", None)
            catalog_fn = getattr(a, "list_session_model_picker_providers", None)
            if not settings_fn or not catalog_fn:
                title, template = "模型菜单不可用", "grey"
                elements = [plain("当前 Hermes 缺少只读模型目录接口，请使用 /model。")]
            else:
                settings = await asyncio.to_thread(settings_fn, source)
                providers = await asyncio.to_thread(catalog_fn, source, max_models=None, include_moa=False)
                if settings is None or providers is None:
                    return
                page = max(0, int(args[0]))
                choices = [(p["slug"], model) for p in providers for model in p["models"]]
                page_rows = choices[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
                title = "切换模型"
                elements.append(md(f"当前会话 · **{settings['provider']} / {settings['model']}**"))
                elements.append({"tag": "hr"})
                grouped: dict[str, list[str]] = {}
                for provider, model in page_rows:
                    grouped.setdefault(provider, []).append(model)
                for index, (provider, models) in enumerate(grouped.items()):
                    if index:
                        elements.append({"tag": "hr"})
                    elements.append(md(f"<font color='grey'>{provider}</font>"))
                    for model in models:
                        is_current = settings["provider"] == provider and settings["model"] == model
                        right = (
                            [md("<font color='green'>✓ 当前</font>")]
                            if is_current
                            else [btn("选择", ("select_model", provider, model))]
                        )
                        elements.append(row([md(f"**{model}**")], right))
                elements.append(note(f"仅修改当前会话，不改变全局默认模型 · 第 {page + 1} 页"))
                elements.append(
                    actions(*pager(page, "model", len(choices) > (page + 1) * PAGE_SIZE), btn("刷新目录", ("model", 0)))
                )
        elif kind == "session":
            page = max(0, int(args[0]))
            rows, more = await self.host.sessions(source, page)
            title = "切换会话"
            elements.append(md("**选择后先预览消息，再确认切换**"))
            elements.append({"tag": "hr"})
            for index, item in enumerate(rows):
                sid = item["id"]
                label = str(item.get("title") or sid)
                self._titles[sid] = label
                meta = " · ".join(
                    part
                    for part in (
                        relative_time(item.get("last_active")),
                        f"{item['message_count']} 条消息" if item.get("message_count") else "",
                    )
                    if part
                )
                text = f"**{label}**" + (f"\n<font color='grey'>{meta}</font>" if meta else "")
                if index:
                    elements.append({"tag": "hr"})
                if sid == current:
                    elements.append(row([md(text)], [md("<font color='green'>当前</font>")]))
                else:
                    elements.append(row([md(text)], [btn("查看", ("history", sid, 0))]))
            elements.append(note(f"仅显示本私聊的会话 · 第 {page + 1} 页"))
            elements.append(
                actions(
                    *pager(page, "session", more),
                    btn("新建会话", ("select_new",)),
                    btn("刷新", ("session", 0)),
                )
            )
        elif kind == "history":
            sid, page = args
            history_rows, more = await self.host.history(source, sid, page)
            title = "会话消息"
            elements.append(md(f"**{self._titles.get(sid, sid)}**\n<font color='grey'>{sid}</font>"))
            if not history_rows:
                elements.append(plain("没有可显示的普通对话。"))
            for role, text in history_rows:
                elements.append(
                    {
                        "tag": "column_set",
                        "flex_mode": "none",
                        "background_style": "grey" if role == "user" else "default",
                        "columns": [
                            {
                                "tag": "column",
                                "width": "weighted",
                                "weight": 1,
                                "vertical_align": "top",
                                "elements": [
                                    md(f"<font color='grey'>{'你' if role == 'user' else 'Hermes'}</font>"),
                                    plain(text[:800]),
                                ],
                            }
                        ],
                    }
                )
            elements.append(note(f"只读预览，翻页不改变当前会话 · 第 {page + 1} 页"))
            buttons = []
            if page:
                buttons.append(btn("上一页", ("history", sid, page - 1)))
            if more:
                buttons.append(btn("下一页", ("history", sid, page + 1)))
            if sid != current:
                buttons.append(btn("切换到此会话", ("select_session", sid), "primary"))
            buttons.append(btn("返回列表", ("session", 0)))
            elements.append(actions(*buttons))
        elif kind in {"select_model", "select_session", "select_new"}:
            if kind == "select_new":
                title, template = "新建会话？", "orange"
                elements = [
                    plain("将开始新的对话上下文。当前会话历史仍保留，可从会话列表切回。"),
                ]
                buttons = [btn("取消", ("session", 0), size="medium"), btn("新建", ("confirm_new",), "primary", "medium")]
            elif kind == "select_model":
                provider, model = args
                title, template = "确认切换模型？", "orange"
                elements = [
                    md(f"**{provider} / {model}**"),
                    {"tag": "hr"},
                    plain("切换后，当前会话的上下文可能发送给该提供商。只影响本会话，不修改全局默认。"),
                ]
                buttons = [
                    btn("取消", ("model", 0), size="medium"),
                    btn("确认切换", ("confirm_model", provider, model), "primary", "medium"),
                ]
            else:
                title, template = "确认切换会话？", "orange"
                elements = [
                    md(f"**{args[0]}**"),
                    plain("之后的消息将进入该会话的上下文。当前会话仍保留，可随时切回。"),
                ]
                buttons = [
                    btn("取消", ("session", 0), size="medium"),
                    btn("确认切换", ("confirm_session", args[0]), "primary", "medium"),
                ]
            elements.append(actions(*buttons))
        else:
            return

        self._prune()
        # A new generation retires every previous button for this owner/chat.
        for token, state in list(self.tokens.items()):
            if state.owner.ids[0] == owner.ids[0] and state.owner.source.chat_id == source.chat_id:
                del self.tokens[token]
        if not generated:
            return
        card = {
            "config": {"wide_screen_mode": True, "update_multi": True, "enable_forward": False},
            "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
            "elements": elements,
        }
        if message:
            await a.cards._client.update_card_message(message, card)
        else:
            message = await a.cards._client.send_card_to_chat(source.chat_id, card)
        if not message:
            return
        for state in generated.values():
            state.message = message
        self.tokens.update(generated)
        self._prune()

    async def click(self, event: Any) -> None:
        try:
            token = field(field(event, "action"), "value", {}).get("hermes_menu_token")
            state = self.tokens.get(token)
            if state is None:
                return
            chat = state.owner.source.chat_id
            lock = self._locks.setdefault(chat, asyncio.Lock())
            async with lock:
                state = self.tokens.get(token)
                if state is None or state.expires <= time.monotonic():
                    return
                context = field(event, "context")
                if (
                    field(context, "open_chat_id") != chat
                    or field(context, "open_message_id") != state.message
                    or not self.authorized(state.owner, identity(field(event, "operator")))
                ):
                    return
                del self.tokens[token]
                kind, *args = state.action
                if kind.startswith("confirm_"):
                    for sibling, entry in list(self.tokens.items()):
                        if entry.message == state.message and entry.owner.ids == state.owner.ids:
                            del self.tokens[sibling]
                    if (
                        self.host.busy(state.owner.source)
                        or await self.host.current(state.owner.source) != state.current
                    ):
                        await self.adapter.cards._client.update_card_message(
                            state.message,
                            {
                                "config": {"wide_screen_mode": True, "update_multi": True},
                                "header": {
                                    "template": "wathet",
                                    "title": {"tag": "plain_text", "content": "菜单已失效"},
                                },
                                "elements": [
                                    {
                                        "tag": "div",
                                        "text": {
                                            "tag": "plain_text",
                                            "content": "会话忙碌或已改变，请等待任务结束后重新打开菜单。",
                                        },
                                    }
                                ],
                            },
                        )
                        return
                    if kind == "confirm_model":
                        provider, model = args
                        # The native flag parser is whitespace based. Reject ambiguous raw ids,
                        # never normalize or silently turn them into flags.
                        if any(not v or any(c.isspace() for c in v) or v.startswith("-") for v in (provider, model)):
                            return
                        command = f"/model --session --provider {provider} {model}"
                    elif kind == "confirm_session":
                        command = "/resume " + shlex.quote(args[0])
                    elif kind == "confirm_new":
                        command = "/new"
                    else:
                        return
                    ids = state.owner.ids
                    await self.adapter._dispatch_synthetic_event(
                        text=command,
                        message_type=MessageType.COMMAND,
                        chat_id=chat,
                        sender_id=SimpleNamespace(open_id=ids[0], user_id=ids[1], union_id=ids[2]),
                        event_chat_type="p2p",
                        message_id="menu-" + secrets.token_hex(16),
                        raw_message={
                            "_hermes_menu_expected": state.current,
                            "_hermes_menu_reply_to": state.message,
                        },
                    )
                else:
                    await self.render(state.owner, state.current, state.action, state.message)
        except Exception:
            log.exception("Bot menu action refused or unavailable")
