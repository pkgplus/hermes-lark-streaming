"""Read-only compatibility bridge must scope before enumeration or transcript reads."""

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from gateway.config import Platform
from gateway.session import SessionSource


def test_bridge_exists():
    from hermes_lark_streaming.menu_host import MenuHost

    assert callable(MenuHost)


@pytest.mark.asyncio
async def test_history_checks_every_ancestor_before_reading_and_pages_visible_rows():
    from hermes_lark_streaming.menu_host import MenuHost

    source = SessionSource(platform=Platform.FEISHU, chat_id="oc", chat_type="dm", user_id="u")
    db = NS(
        _resume_lineage_ids=Mock(return_value=["root", "tip"]),
        _dedupe_display_generations=lambda rows, **kwargs: rows,
        _row_to_message_dict=lambda row, **kwargs: row,
        _fetch_conversation_rows=Mock(
            return_value=[
                *[{"role": "tool", "content": "hidden"} for _ in range(10)],
                *[{"role": "user", "content": str(i)} for i in range(19)],
            ]
        ),
        close=Mock(),
    )
    runner = NS(
        _resume_target_allowed=AsyncMock(return_value=True),
        _session_key_for_source=lambda s: "lane",
        config=NS(multiplex_profiles=False),
    )
    host = MenuHost(NS(gateway_runner=runner))
    host._reader = Mock(return_value=db)
    rows, more = await host.history(source, "tip", 1)
    assert rows == [("user", str(i)) for i in range(8, 16)]
    assert more
    assert [c.args[1] for c in runner._resume_target_allowed.call_args_list] == ["tip", "root", "tip"]
    assert db._fetch_conversation_rows.call_args.args[0] == ["root", "tip"]
    assert db._fetch_conversation_rows.call_args.kwargs["with_compacted"]
    runner._resume_target_allowed.side_effect = [True, False]
    db._fetch_conversation_rows.reset_mock()
    with pytest.raises(PermissionError):
        await host.history(source, "tip", 0)
    db._fetch_conversation_rows.assert_not_called()


@pytest.mark.asyncio
async def test_session_enumeration_is_scoped_before_owner_filter():
    from hermes_lark_streaming.menu_host import MenuHost

    source = NS(platform=Platform.FEISHU)
    db = NS(list_sessions_rich=Mock(return_value=[{"id": "mine"}, {"id": "other"}]), close=Mock())
    runner = NS(
        _session_key_for_source=lambda s: "scoped-lane",
        _resume_target_allowed=AsyncMock(side_effect=[True, False]),
        config=NS(multiplex_profiles=False),
    )
    host = MenuHost(NS(gateway_runner=runner))
    host._reader = Mock(return_value=db)
    rows, more = await host.sessions(source, 0)
    assert rows == [{"id": "mine"}]
    assert db.list_sessions_rich.call_args.kwargs["session_key"] == "scoped-lane"
    assert db.list_sessions_rich.call_args.kwargs["source"] == "feishu"
    assert not more


@pytest.mark.asyncio
async def test_current_lookup_does_not_allocate_and_idle_shared_authority_allows_switch():
    from hermes_lark_streaming.menu_host import MenuHost

    runner = NS(
        config=NS(multiplex_profiles=False),
        _session_key_for_source=lambda s: "lane",
        async_session_store=NS(lookup_by_session_key=AsyncMock(return_value=NS(session_id="sid"))),
        _running_agents={},
    )
    adapter = NS(gateway_runner=runner, _active_sessions={})
    host = MenuHost(adapter)
    assert await host.current(NS()) == "sid"
    assert not host.busy(NS())
    runner.session_authority = NS(sessions={}, executing={})
    assert not host.busy(NS())
    runner.config.multiplex_profiles = True
    with pytest.raises(RuntimeError, match="multiplex"):
        await host.current(NS())
