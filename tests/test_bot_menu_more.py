import asyncio
from unittest.mock import AsyncMock

import pytest
from gateway.platforms.base import MessageEvent
from test_bot_menu import action_event, builtin, inbound, menu_event, tokens
from test_bot_menu import adapter as adapter


@pytest.mark.asyncio
async def test_real_native_ingress_refuses_changed_session_after_sender_lookup(adapter, monkeypatch):
    from unittest.mock import patch

    event = MessageEvent(
        text="/resume sid-old", source=inbound().source, raw_message={"_hermes_menu_expected": "sid-current"}
    )
    adapter.menu.host.current.return_value = "sid-changed"
    with patch.object(builtin.FeishuAdapter, "handle_message", new=AsyncMock()) as parent:
        await adapter._handle_message_with_guards(event)
        parent.assert_not_called()


def test_plain_projection_excludes_native_compaction_marker():
    from hermes_lark_streaming.bot_menu import plain_messages

    assert plain_messages([{"role": "user", "content": "private summary", "_compressed_summary": True}]) == []


def test_webhook_registry_extends_without_mutating_core(adapter):
    assert adapter._WEBHOOK_EVENT_HANDLERS["application.bot.menu_v6"] == "_on_bot_menu"
    assert "application.bot.menu_v6" not in builtin.FeishuAdapter._WEBHOOK_EVENT_HANDLERS


@pytest.mark.asyncio
async def test_native_model_flags_preserve_raw_id_and_explicit_provider(adapter):
    from hermes_cli.model_switch import parse_model_switch_args

    await adapter.menu.open(menu_event())
    await adapter.menu.click(action_event(tokens(adapter)[("select_model", "custom", "raw/next")]))
    await adapter.menu.click(action_event(tokens(adapter)[("confirm_model", "custom", "raw/next")]))
    text = adapter._dispatch_synthetic_event.call_args.kwargs["text"]
    parsed = parse_model_switch_args(text.removeprefix("/model "))
    assert parsed.scope == "session"
    assert parsed.target == "raw/next"
    assert parsed.explicit_provider == "custom"


@pytest.mark.asyncio
async def test_confirm_retires_sibling_buttons_and_concurrent_replay(adapter):
    await adapter.menu.open(menu_event())
    await adapter.menu.click(action_event(tokens(adapter)[("select_model", "custom", "raw/next")]))
    token = tokens(adapter)[("confirm_model", "custom", "raw/next")]
    await asyncio.gather(adapter.menu.click(action_event(token)), adapter.menu.click(action_event(token)))
    assert adapter._dispatch_synthetic_event.await_count == 1
    assert not adapter.menu.tokens


@pytest.mark.asyncio
async def test_open_serializes_same_dm_and_does_not_accumulate_locks(adapter):
    entered, release = asyncio.Event(), asyncio.Event()

    async def render(*args):
        entered.set()
        await release.wait()

    adapter.menu.render = AsyncMock(side_effect=render)
    first = asyncio.create_task(adapter.menu.open(menu_event()))
    await entered.wait()
    second = asyncio.create_task(adapter.menu.open(menu_event()))
    try:
        await asyncio.sleep(0)
        assert adapter.menu.render.await_count == 1
    finally:
        release.set()
        await asyncio.gather(first, second)
    assert not adapter.menu._locks


@pytest.mark.asyncio
async def test_real_sdk_menu_and_card_click_reach_plugin_without_replacing_native_routes(adapter):
    import json

    adapter._loop = asyncio.get_running_loop()
    handler = adapter._build_event_handler()
    menu = {
        "schema": "2.0",
        "header": {"event_type": "application.bot.menu_v6"},
        "event": {
            "event_key": "session",
            "operator": {"operator_id": {"open_id": "ou_owner", "user_id": "tenant", "union_id": "on_union"}},
        },
    }
    handler._do_without_validation(json.dumps(menu).encode())
    for _ in range(100):
        if adapter.menu.tokens:
            break
        await asyncio.sleep(0.01)
    token = tokens(adapter)[("history", "sid-old", 0)]
    click = {
        "schema": "2.0",
        "header": {"event_type": "card.action.trigger"},
        "event": {
            "operator": {"open_id": "ou_owner", "user_id": "tenant", "union_id": "on_union"},
            "context": {"open_chat_id": "oc_dm", "open_message_id": "om_card"},
            "action": {"tag": "button", "value": {"hermes_menu_token": token}},
        },
    }
    assert handler._do_without_validation(json.dumps(click).encode()) is not None
    for _ in range(100):
        if adapter.menu.host.history.called:
            break
        await asyncio.sleep(0.01)
    adapter.menu.host.history.assert_awaited_once()
    assert not adapter._dispatch_synthetic_event.called


def test_unauthorized_inbound_does_not_seed_owner_mapping(adapter):
    adapter.menu.owners.clear()
    adapter._is_sender_authorized.return_value = False
    adapter.menu.observe(inbound())
    assert not adapter.menu.owners
