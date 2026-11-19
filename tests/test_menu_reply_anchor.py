"""Reply identity is separate from native ingress/dedup identity."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource

from hermes_lark_streaming.adapter import ACTIVE, FeishuStreamingAdapter, builtin


@pytest.mark.asyncio
@pytest.mark.parametrize("message_id", ["menu-random", "card-token", "random-uuid", "om_real"])
@pytest.mark.parametrize("marked", [False, True])
async def test_only_menu_metadata_overrides_reply_not_event_identity(monkeypatch, message_id, marked):
    adapter = FeishuStreamingAdapter(PlatformConfig(enabled=True, extra={"app_id": "app", "app_secret": "secret"}))
    source = SessionSource(platform=Platform.FEISHU, chat_id="oc_dm", chat_type="dm", user_id="tenant")
    raw = {"_hermes_menu_reply_to": "om_menu_card"}
    if marked:
        raw["_hermes_menu_expected"] = "sid-current"
    event = MessageEvent(text="/model", source=source, message_id=message_id, raw_message=raw)

    @asynccontextmanager
    async def guard(*args):
        yield

    adapter.menu.host = SimpleNamespace(
        busy=Mock(return_value=False), current=AsyncMock(return_value="sid-current"), command_guard=guard
    )
    handler = AsyncMock(return_value="model selected")
    adapter._message_handler = handler
    adapter.cards._client.send_card_to_chat = AsyncMock(return_value="om_feedback")
    # Use the same native inline delivery for the non-menu control; no routing mock.
    monkeypatch.setattr(builtin.FeishuAdapter, "handle_message", builtin.FeishuAdapter._dispatch_inline_reply)
    try:
        await adapter.handle_message(event)
        handler.assert_awaited_once_with(event)
        assert event.message_id == message_id
        adapter.cards._client.send_card_to_chat.assert_awaited_once()
        assert adapter.cards._client.send_card_to_chat.call_args.kwargs["reply_to_message_id"] == (
            "om_menu_card" if marked else message_id
        )
    finally:
        ACTIVE.discard(adapter)
