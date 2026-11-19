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
MENU_KEYS = frozenset({"model", "session", "current"})


def field(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def identity(obj: Any) -> tuple[str, str, str]:
    return tuple(str(field(obj, key, "") or "") for key in ("open_id", "user_id", "union_id"))  # type: ignore[return-value]


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
            if owner is None or not self.authorized(owner, ids):
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
        buttons: list[tuple[str, tuple[Any, ...]]] = []
        title, body = "Hermes", ""
        if kind == "model":
            settings_fn = getattr(a, "get_session_model_settings", None)
            catalog_fn = getattr(a, "list_session_model_picker_providers", None)
            if not settings_fn or not catalog_fn:
                title, body = "模型菜单不可用", "当前 Hermes 缺少只读模型目录接口，请使用 /model。"
            else:
                settings = await asyncio.to_thread(settings_fn, source)
                providers = await asyncio.to_thread(catalog_fn, source, max_models=None, include_moa=False)
                if settings is None or providers is None:
                    return
                choices = [(p["slug"], model) for p in providers for model in p["models"]]
                page = max(0, int(args[0]))
                title = "切换模型（仅当前会话）"
                body = f"当前：{settings['provider']} / {settings['model']}\n第 {page + 1} 页"
                for provider, model in choices[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
                    buttons.append((f"{provider} / {model}", ("select_model", provider, model)))
                self._pages(buttons, "model", page, len(choices) > (page + 1) * PAGE_SIZE)
                buttons.append(("刷新模型目录", ("model", 0)))
        elif kind == "session":
            page = max(0, int(args[0]))
            rows, more = await self.host.sessions(source, page)
            title, body = "会话列表", f"当前：{current or '尚未创建'}\n第 {page + 1} 页（仅当前私聊）"
            for row in rows:
                sid = row["id"]
                label = str(row.get("title") or sid)
                buttons.extend([(f"切换：{label}", ("select_session", sid)), (f"预览：{label}", ("history", sid, 0))])
            self._pages(buttons, "session", page, more)
            buttons.append(("新建会话", ("select_new",)))
            buttons.append(("刷新会话", ("session", 0)))
        elif kind == "history":
            sid, page = args
            history_rows, more = await self.host.history(source, sid, page)
            title = f"历史预览 · 第 {page + 1} 页"
            body = (
                "\n\n".join(f"{'用户' if role == 'user' else '助手'}：\n{text}" for role, text in history_rows)
                or "没有可显示的普通对话。"
            )
            if page:
                buttons.append(("上一页", ("history", sid, page - 1)))
            if more:
                buttons.append(("下一页", ("history", sid, page + 1)))
            if sid != current:
                buttons.append(("切换到此会话", ("select_session", sid)))
            buttons.append(("返回会话列表", ("session", 0)))
        elif kind in {"select_model", "select_session", "select_new"}:
            title = "确认切换？"
            body = " / ".join(str(v) for v in args) + "\n只提交原生命令；权限、策略及费用确认仍由 Hermes 执行。"
            if kind == "select_model":
                body += "\n切换后，当前会话上下文可能发送给所选提供商；不会修改全局默认模型。"
            elif kind == "select_new":
                title, body = "新建会话？", "开始新的对话上下文，当前会话历史仍保留，可从会话列表恢复。"
            buttons = [("确认切换", (kind.replace("select_", "confirm_"), *args)), ("取消", ("current", 0))]
        elif kind == "current":
            fn = getattr(a, "get_session_model_settings", None)
            settings = await asyncio.to_thread(fn, source) if fn else None
            title, body = "当前会话", f"会话：{current or '尚未创建'}"
            if settings:
                body += f"\n提供商：{settings['provider']}\n模型：{settings['model']}"
            buttons = [("切换模型", ("model", 0)), ("会话列表", ("session", 0))]
            if current:
                buttons.append(("预览当前会话", ("history", current, 0)))
        else:
            return
        self._prune()
        # A new generation retires every previous button for this owner/chat.
        for token, state in list(self.tokens.items()):
            if state.owner.ids[0] == owner.ids[0] and state.owner.source.chat_id == source.chat_id:
                del self.tokens[token]
        generated: dict[str, MenuToken] = {}
        elements: list[dict[str, Any]] = [{"tag": "div", "text": {"tag": "plain_text", "content": body}}]
        for label, target in buttons:
            token = secrets.token_urlsafe(24)
            generated[token] = MenuToken(owner, message, current, target, time.monotonic() + TOKEN_TTL)
            elements.append(
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": label[:80]},
                            "value": {"hermes_menu_token": token},
                        }
                    ],
                }
            )
        card = {
            "config": {"wide_screen_mode": True, "update_multi": True, "enable_forward": False},
            "header": {"title": {"tag": "plain_text", "content": title}},
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

    @staticmethod
    def _pages(buttons: list[Any], kind: str, page: int, more: bool) -> None:
        if page:
            buttons.append(("上一页", (kind, page - 1)))
        if more:
            buttons.append(("下一页", (kind, page + 1)))

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
                                "elements": [
                                    {
                                        "tag": "div",
                                        "text": {
                                            "tag": "plain_text",
                                            "content": "会话忙碌或已改变，请等待任务结束后重新打开菜单。",
                                        },
                                    }
                                ]
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
                        raw_message={"_hermes_menu_expected": state.current},
                    )
                else:
                    await self.render(state.owner, state.current, state.action, state.message)
        except Exception:
            log.exception("Bot menu action refused or unavailable")
