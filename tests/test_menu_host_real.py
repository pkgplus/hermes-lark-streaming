"""Exercise real Hermes SessionDB read-only lineage and owner verification."""

import asyncio
from types import SimpleNamespace as NS

import pytest
from gateway.config import Platform
from gateway.session import SessionSource
from gateway.slash_commands_session import GatewaySessionCommandsMixin
from hermes_state import SessionDB

from hermes_lark_streaming.menu_host import MenuHost


@pytest.mark.asyncio
async def test_real_readonly_store_complete_history_and_native_owner_guard(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        origin = dict(user_id="tenant", chat_id="oc_dm", chat_type="dm", session_key="lane")
        db.create_session("root", "feishu", **origin)
        for i in range(13):
            db.append_message("root", "user", f"q{i}")
        db.end_session("root", "compression")
        db.create_session("tip", "feishu", parent_session_id="root", **origin)
        db.append_message("tip", "user", "secret summary", _compressed_summary=True)
        for i in range(13, 27):
            db.append_message("tip", "assistant", f"a{i}", reasoning="private reasoning")
        db.create_session("foreign", "feishu", user_id="other", chat_id="oc_else", chat_type="dm", session_key="other")
        db.append_message("foreign", "user", "foreign secret")
        runner = GatewaySessionCommandsMixin()
        runner.config = NS(multiplex_profiles=False)
        runner._session_key_for_source = lambda s: "lane"
        runner._resolve_profile_home_for_source = lambda s: tmp_path
        runner._gateway_session_origin_for_id = lambda sid: None

        async def get_session(sid):
            return await asyncio.to_thread(db.get_session, sid)

        runner._session_db = NS(get_session=get_session)
        host = MenuHost(NS(gateway_runner=runner))
        source = SessionSource(platform=Platform.FEISHU, chat_id="oc_dm", chat_type="dm", user_id="tenant")
        reader = host._reader(source)
        assert reader.read_only
        reader.close()
        combined = []
        for page in range(4):
            rows, more = await host.history(source, "tip", page)
            combined.extend(rows)
            assert more == (page < 3)
        assert combined == [("user", f"q{i}") for i in range(13)] + [("assistant", f"a{i}") for i in range(13, 27)]
        with pytest.raises(PermissionError):
            await host.history(source, "foreign", 0)
        rows, _ = await host.sessions(source, 0)
        assert {row["id"] for row in rows} == {"tip"}
        # A second compression child is omitted by get_compression_lineage,
        # but _resume_lineage_ids (the actual materializer) still includes root.
        db.end_session("foreign", "compression")
        db.create_session("first-foreign-child", "feishu", parent_session_id="foreign")
        db.create_session("second", "feishu", parent_session_id="foreign", **origin)
        db.append_message("second", "user", "second visible")
        assert db.get_compression_lineage("second") == ["second"]
        assert db._resume_lineage_ids("second") == ["foreign", "second"]
        with pytest.raises(PermissionError):
            await host.history(source, "second", 0)
        db.create_session(
            "owned-branch", "feishu", parent_session_id="foreign", model_config={"_branched_from": "foreign"}, **origin
        )
        db.append_message("owned-branch", "user", "branch visible")
        db.append_message("owned-branch", "user", [{"type": "text", "text": "structured visible"}])
        rows, more = await host.history(source, "owned-branch", 0)
        assert rows == [("user", "branch visible"), ("user", "structured visible")]
        assert not more
    finally:
        db.close()
