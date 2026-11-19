"""Bot menu security/state tests; SDK dispatch and native ingress are exercised too."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource

from hermes_lark_streaming.adapter import FeishuStreamingAdapter, builtin


def ns(value):
    if isinstance(value, dict):
        return NS(**{k: ns(v) for k, v in value.items()})
    return value


def inbound(open_id="ou_owner", user_id="tenant", union_id="on_union", chat="oc_dm"):
    return MessageEvent(
        text="hello",
        message_id="om_in",
        source=SessionSource(
            platform=Platform.FEISHU,
            chat_id=chat,
            chat_type="dm",
            user_id=user_id,
            user_id_alt=union_id,
        ),
        raw_message=ns(
            {
                "event": {
                    "sender": {
                        "sender_type": "user",
                        "sender_id": {"open_id": open_id, "user_id": user_id, "union_id": union_id},
                    },
                    "message": {"chat_id": chat, "chat_type": "p2p"},
                }
            }
        ),
    )


def menu_event(key="model", actor=None):
    return ns(
        {
            "event": {
                "event_key": key,
                "operator": {
                    "operator_id": actor or {"open_id": "ou_owner", "user_id": "tenant", "union_id": "on_union"}
                },
            }
        }
    )


def action_event(token, actor="ou_owner", chat="oc_dm", message="om_card"):
    event = ns(
        {
            "operator": {"open_id": actor, "user_id": "tenant", "union_id": "on_union"},
            "context": {"open_chat_id": chat, "open_message_id": message},
        }
    )
    event.action = NS(value={"hermes_menu_token": token})
    return event


@pytest.fixture
def adapter(monkeypatch):
    builtin._load_lark_oapi()
    a = FeishuStreamingAdapter(PlatformConfig(enabled=True, extra={"app_id": "app", "app_secret": "secret"}))
    a._is_sender_authorized = Mock(return_value=True)
    a._admit_synthetic = Mock(return_value=None)
    a.cards._client.send_card_to_chat = AsyncMock(return_value="om_card")
    a.cards._client.update_card_message = AsyncMock()
    a.menu.host = NS(
        current=AsyncMock(return_value="sid-current"),
        recent_dm_sources=AsyncMock(return_value=[]),
        sessions=AsyncMock(return_value=([{"id": "sid-old", "title": "Older"}], False)),
        history=AsyncMock(return_value=([("user", "old question"), ("assistant", "old answer")], False)),
        busy=Mock(return_value=False),
    )
    a.get_session_model_settings = Mock(
        return_value={"model": "raw/model", "provider": "custom", "session_key": "lane"}
    )
    a.list_session_model_picker_providers = Mock(
        return_value=[
            {"slug": "custom", "name": "My Provider", "models": ["raw/model", "raw/next"], "is_current": True}
        ]
    )
    a._dispatch_synthetic_event = AsyncMock()
    a.menu.observe(inbound())
    return a


def tokens(a):
    return {entry.action: token for token, entry in a.menu.tokens.items()}


@pytest.mark.asyncio
async def test_menu_uses_fresh_native_catalog_and_session_only_command(adapter):
    await adapter.menu.open(menu_event())
    adapter.list_session_model_picker_providers.assert_called_once()
    assert adapter.list_session_model_picker_providers.call_args.kwargs == {"max_models": None, "include_moa": False}
    token = tokens(adapter)[("select_model", "custom", "raw/next")]
    await adapter.menu.click(action_event(token))
    assert not adapter._dispatch_synthetic_event.called  # explicit confirmation first
    await adapter.menu.click(action_event(tokens(adapter)[("confirm_model", "custom", "raw/next")]))
    assert adapter._dispatch_synthetic_event.call_args.kwargs["text"] == "/model --session --provider custom raw/next"
    await adapter.menu.open(menu_event())
    assert adapter.list_session_model_picker_providers.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actor,chat,message",
    [("ou_other", "oc_dm", "om_card"), ("ou_owner", "oc_else", "om_card"), ("ou_owner", "oc_dm", "om_else")],
)
async def test_card_tokens_bind_actor_chat_message(adapter, actor, chat, message):
    await adapter.menu.open(menu_event())
    token = tokens(adapter)[("select_model", "custom", "raw/next")]
    before = len(adapter.menu.tokens)
    await adapter.menu.click(action_event(token, actor, chat, message))
    assert len(adapter.menu.tokens) == before
    assert not adapter._dispatch_synthetic_event.called


@pytest.mark.asyncio
async def test_tokens_expire_replay_and_stale_session_fail_closed(adapter):
    await adapter.menu.open(menu_event())
    token = tokens(adapter)[("select_model", "custom", "raw/next")]
    adapter.menu.tokens[token].expires = 0
    await adapter.menu.click(action_event(token))
    assert not adapter.cards._client.update_card_message.called
    await adapter.menu.open(menu_event())
    token = tokens(adapter)[("select_model", "custom", "raw/next")]
    await adapter.menu.click(action_event(token))
    count = adapter.cards._client.update_card_message.await_count
    await adapter.menu.click(action_event(token))
    assert adapter.cards._client.update_card_message.await_count == count
    adapter.menu.host.current.return_value = "sid-new"
    await adapter.menu.click(action_event(tokens(adapter)[("confirm_model", "custom", "raw/next")]))
    assert not adapter._dispatch_synthetic_event.called


@pytest.mark.asyncio
async def test_busy_confirm_never_dispatches(adapter):
    await adapter.menu.open(menu_event("session"))
    await adapter.menu.click(action_event(tokens(adapter)[("history", "sid-old", 0)]))
    await adapter.menu.click(action_event(tokens(adapter)[("select_session", "sid-old")]))
    adapter.menu.host.busy.return_value = True
    await adapter.menu.click(action_event(tokens(adapter)[("confirm_session", "sid-old")]))
    assert not adapter._dispatch_synthetic_event.called


@pytest.mark.asyncio
async def test_history_preview_changes_no_route_and_pages(adapter):
    adapter.menu.host.history.return_value = ([("user", "<b>plain</b>"), ("assistant", "answer")], True)
    await adapter.menu.open(menu_event("session"))
    await adapter.menu.click(action_event(tokens(adapter)[("history", "sid-old", 0)]))
    card = adapter.cards._client.update_card_message.call_args.args[1]
    assert "plain_text" in json.dumps(card)
    await adapter.menu.click(action_event(tokens(adapter)[("history", "sid-old", 1)]))
    assert adapter.menu.host.history.call_args.args[-1] == 1
    assert not adapter._dispatch_synthetic_event.called


@pytest.mark.asyncio
async def test_unknown_actor_id_tiers_cannot_be_substituted_and_auth_rechecked(adapter):
    await adapter.menu.open(menu_event(actor={"user_id": "ou_owner"}))
    assert not adapter.cards._client.send_card_to_chat.called
    await adapter.menu.open(menu_event(actor={"open_id": "ou_owner", "user_id": "other"}))
    assert not adapter.cards._client.send_card_to_chat.called
    adapter._is_sender_authorized.return_value = False
    await adapter.menu.open(menu_event())
    assert not adapter.cards._client.send_card_to_chat.called


@pytest.mark.asyncio
async def test_group_and_unverified_mapping_not_observed(adapter):
    a = FeishuStreamingAdapter(PlatformConfig(extra={}))
    group = inbound()
    group.source.chat_type = "group"
    a.menu.observe(group)
    a.menu.observe(MessageEvent(text="fake", source=inbound().source, raw_message={}))
    assert not a.menu.owners


def test_real_sdk_dispatches_official_menu_shape_and_preserves_callbacks(adapter):
    seen = []
    adapter._on_bot_menu = lambda data: seen.append(data.event.operator.operator_id.open_id)
    adapter._on_card_action_trigger = lambda data: adapter._card_response()
    handler = adapter._build_event_handler()
    payload = {
        "schema": "2.0",
        "header": {"event_id": "ev", "event_type": "application.bot.menu_v6"},
        "event": {
            "event_key": "model",
            "operator": {"operator_id": {"open_id": "ou_owner", "user_id": "tenant", "union_id": "on_union"}},
        },
    }
    handler._do_without_validation(json.dumps(payload).encode())
    assert seen == ["ou_owner"]
    payload["header"]["event_type"] = "card.action.trigger"
    payload["event"] = {"operator": {"open_id": "ou_owner"}, "action": {"tag": "button", "value": {}}}
    assert handler._do_without_validation(json.dumps(payload).encode()) is not None
    assert adapter._CARD_ACTION_ROUTES[1:] == builtin.FeishuAdapter._CARD_ACTION_ROUTES


def test_plain_projection_excludes_internal_messages_and_only_keeps_text():
    from hermes_lark_streaming.bot_menu import plain_messages

    rows = [
        {"role": "system", "content": "secret"},
        {"role": "tool", "content": "secret"},
        {"role": "assistant", "content": "secret", "tool_calls": [{}]},
        {"role": "user", "content": "summary", "is_summary": True},
        {"role": "user", "content": "hidden", "display_kind": "handoff"},
        {"role": "assistant", "content": "answer", "reasoning": "secret"},
        {
            "role": "user",
            "content": [{"type": "text", "text": "question"}, {"type": "image_url", "image_url": {"url": "secret"}}],
        },
    ]
    assert plain_messages(rows) == [("assistant", "answer"), ("user", "question")]


@pytest.mark.asyncio
async def test_menu_restores_dm_binding_lost_to_restart(adapter):
    # A fresh process starts with no in-memory owner map; state.db still has the DM.
    adapter.menu.owners.clear()
    adapter.menu.host.recent_dm_sources.return_value = [
        SessionSource(platform=Platform.FEISHU, chat_id="oc_dm", chat_type="dm", user_id="ou_owner", user_id_alt="on_union")
    ]
    await adapter.menu.open(menu_event())
    adapter.cards._client.send_card_to_chat.assert_awaited_once()
    assert "ou_owner" in adapter.menu.owners


@pytest.mark.asyncio
async def test_restore_never_adopts_another_users_dm(adapter):
    adapter.menu.owners.clear()
    adapter.menu.host.recent_dm_sources.return_value = [
        SessionSource(platform=Platform.FEISHU, chat_id="oc_other", chat_type="dm", user_id="ou_someone_else", user_id_alt="on_other")
    ]
    await adapter.menu.open(menu_event())
    adapter.cards._client.send_card_to_chat.assert_not_called()
    assert not adapter.menu.owners
