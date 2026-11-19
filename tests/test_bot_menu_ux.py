"""Menu acceptance checks for the preview/confirm workflow."""

import json

import pytest
from test_bot_menu import action_event, menu_event, tokens
from test_bot_menu import adapter as adapter


@pytest.mark.asyncio
async def test_history_can_confirm_switch_without_returning_to_list(adapter):
    await adapter.menu.open(menu_event("session"))
    await adapter.menu.click(action_event(tokens(adapter)[("history", "sid-old", 0)]))
    actions = tokens(adapter)
    assert ("select_session", "sid-old") in actions
    assert not adapter._dispatch_synthetic_event.called
    await adapter.menu.click(action_event(actions[("select_session", "sid-old")]))
    await adapter.menu.click(action_event(tokens(adapter)[("confirm_session", "sid-old")]))
    assert adapter._dispatch_synthetic_event.call_args.kwargs["text"] == "/resume sid-old"


@pytest.mark.asyncio
async def test_session_list_creates_new_session_only_after_confirmation(adapter):
    await adapter.menu.open(menu_event("session"))
    assert ("select_new",) in tokens(adapter)
    await adapter.menu.click(action_event(tokens(adapter)[("select_new",)]))
    assert not adapter._dispatch_synthetic_event.called
    await adapter.menu.click(action_event(tokens(adapter)[("confirm_new",)]))
    assert adapter._dispatch_synthetic_event.call_args.kwargs["text"] == "/new"


@pytest.mark.asyncio
async def test_model_confirmation_discloses_context_to_selected_provider(adapter):
    await adapter.menu.open(menu_event("model"))
    await adapter.menu.click(action_event(tokens(adapter)[("select_model", "custom", "raw/next")]))
    card = adapter.cards._client.update_card_message.call_args.args[1]
    content = json.dumps(card, ensure_ascii=False)
    assert "上下文" in content
    assert "提供商" in content
    assert "custom" in content


@pytest.mark.asyncio
async def test_preview_card_updates_all_clients_and_disables_forwarding(adapter):
    await adapter.menu.open(menu_event("session"))
    await adapter.menu.click(action_event(tokens(adapter)[("history", "sid-old", 0)]))
    card = adapter.cards._client.update_card_message.call_args.args[1]
    assert card["config"]["update_multi"] is True
    assert card["config"]["enable_forward"] is False


@pytest.mark.asyncio
async def test_fixed_menus_do_not_offer_current_session_entry(adapter):
    from hermes_lark_streaming.bot_menu import MENU_KEYS

    assert {"model", "session"} == MENU_KEYS
    await adapter.menu.open(menu_event("current"))
    adapter.cards._client.send_card_to_chat.assert_not_called()


@pytest.mark.asyncio
async def test_selecting_session_always_opens_its_messages_first(adapter):
    await adapter.menu.open(menu_event("session"))
    assert ("select_session", "sid-old") not in tokens(adapter)
    await adapter.menu.click(action_event(tokens(adapter)[("history", "sid-old", 0)]))
    adapter.menu.host.history.assert_awaited_once_with(
        adapter.menu.owners["ou_owner"].source, "sid-old", 0
    )
    content = json.dumps(adapter.cards._client.update_card_message.call_args.args[1], ensure_ascii=False)
    assert "sid-old" in content
    assert "old question" in content
    assert "old answer" in content
    assert not adapter._dispatch_synthetic_event.called


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["session", "model"])
async def test_cancel_returns_to_relevant_picker_not_current_session(adapter, kind):
    await adapter.menu.open(menu_event(kind))
    if kind == "session":
        await adapter.menu.click(action_event(tokens(adapter)[("history", "sid-old", 0)]))
        await adapter.menu.click(action_event(tokens(adapter)[("select_session", "sid-old")]))
    else:
        await adapter.menu.click(action_event(tokens(adapter)[("select_model", "custom", "raw/next")]))
    assert ("current", 0) not in tokens(adapter)
    assert (kind, 0) in tokens(adapter)
    await adapter.menu.click(action_event(tokens(adapter)[(kind, 0)]))
    assert not adapter._dispatch_synthetic_event.called
