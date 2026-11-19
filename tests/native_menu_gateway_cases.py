"""Native v0.23 gateway/authority admission; no patched runner or state writes."""

import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageType
from gateway.run import GatewayRunner
from gateway.run_runtime import initialize_gateway_runtime
from gateway.runtime_ownership import process_ownership

from hermes_lark_streaming.adapter import ACTIVE, FeishuStreamingAdapter


class TransportAdapter(FeishuStreamingAdapter):
    """Only the remote Feishu transport is replaced, not admission or commands."""

    async def _resolve_sender_profile(self, sender):
        return {"user_id": sender.user_id, "user_name": "Owner", "user_id_alt": sender.open_id}

    async def get_chat_info(self, chat_id):
        return {"name": "DM", "raw_mode": "p2p", "type": "private"}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return SendResult(success=True, message_id=f"out-{len(self.sent)}")

    def supports_native_streaming(self, chat_type=None, metadata=None):
        return False

    async def send_typing(self, chat_id, metadata=None):
        pass

    async def edit_message(self, chat_id, message_id, content, metadata=None):
        return await self.send(chat_id, content, metadata=metadata)


@pytest.fixture
def provider():
    calls = []
    state = SimpleNamespace(hold=None, entered=threading.Event(), release=threading.Event())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.reply({"data": [{"id": "test-before"}, {"id": "test-after"}], "object": "list"})

        def reply(self, body):
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(body)
            if state.hold and state.hold(body):
                state.entered.set()
                assert state.release.wait(20), "provider barrier not released"
            if body.get("stream"):
                chunk = {
                    "id": "test-completion",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": body.get("model"),
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "isolated provider reply"},
                            "finish_reason": "stop",
                        }
                    ],
                }
                payload = ("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.reply(
                {
                    "id": "test-completion",
                    "object": "chat.completion",
                    "created": 1,
                    "model": body.get("model"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "isolated provider reply"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
                }
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        state.url, state.calls = f"http://127.0.0.1:{server.server_port}/v1", calls
        yield state
    finally:
        state.release.set()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest_asyncio.fixture
async def native(tmp_path, monkeypatch, provider):
    tmp_path = Path(os.environ["NATIVE_MENU_TEST_HOME"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("FEISHU_ALLOWED_USERS", "tenant")
    (tmp_path / "config.yaml").write_text(
        f"model:\n  default: test-before\n  provider: custom\n  base_url: {provider.url}\n  api_key: fake-test-key\n"
        "agent:\n  max_turns: 1\n  quiet: true\n"
        "streaming:\n  enabled: false\n"
        "memory:\n  memory_enabled: false\n  user_profile_enabled: false\n"
        "compression:\n  enabled: false\n"
    )
    process_ownership.reserve([tmp_path])
    runner = GatewayRunner(GatewayConfig())
    adapter = TransportAdapter(PlatformConfig(enabled=True, extra={"app_id": "app", "app_secret": "secret"}))
    adapter.sent = []
    runner.adapters[Platform.FEISHU] = adapter
    await initialize_gateway_runtime(runner)
    adapter.gateway_runner = runner
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(
        chat_id="oc_dm", chat_type="dm", user_id="tenant", user_name="Owner", user_id_alt="ou_owner"
    )
    from gateway.session_identity import canonical_identity

    canonical_identity(source, runner=runner, adapter=adapter)
    assert runner._resolve_profile_home_for_source(source) == tmp_path
    first = await runner.async_session_store.get_or_create_session(source)
    old = first.session_id
    fresh = await runner.async_session_store.get_or_create_session(source, force_new=True)
    assert fresh.session_id != old
    try:
        yield SimpleNamespace(runner=runner, adapter=adapter, source=source, old=old, current=fresh.session_id)
    finally:
        tasks = [*adapter._session_tasks.values()]
        for live in runner.session_authority.sessions.values():
            if live.task:
                tasks.append(live.task)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        ACTIVE.discard(adapter)
        for authority in runner.session_authorities:
            authority.db.close()
        process_ownership.release(tmp_path)


def durable_route(native):
    from hermes_state import SessionDB

    reader = SessionDB(native.runner.session_authority.db.db_path, read_only=True)
    try:
        rows = reader.load_gateway_routing_entries(scope=str(native.runner.session_store.sessions_dir.resolve()))
        return json.loads(rows[native.runner._session_key_for_source(native.source)])
    finally:
        reader.close()


async def dispatch(native, text, expected=None, *, marked=True):
    await native.adapter._dispatch_synthetic_event(
        text=text,
        message_type=MessageType.COMMAND if text.startswith("/") else MessageType.TEXT,
        chat_id="oc_dm",
        sender_id=SimpleNamespace(user_id="tenant", open_id="ou_owner", union_id=""),
        event_chat_type="p2p",
        message_id="in-menu",
        raw_message={"_hermes_menu_expected": expected or native.current} if marked else {},
    )
    if marked:
        return
    for _ in range(1500):
        tasks = [task for task in native.adapter._session_tasks.values() if not task.done()]
        if not tasks:
            return
        await asyncio.sleep(0.01)
    pytest.fail("native adapter task did not finish")


@pytest.mark.asyncio
async def test_native_authority_allows_idle_resume_and_reads_back_route(native, provider):
    assert native.runner.session_authority is not None
    await dispatch(native, f"/resume {native.old}")
    entry = await native.runner.async_session_store.lookup_by_session_key(
        native.runner._session_key_for_source(native.source)
    )
    assert entry.session_id == native.old, native.adapter.sent
    assert durable_route(native)["session_id"] == native.old
    assert not native.adapter.menu.host.busy(native.source)
    await dispatch(native, "This turn follows resume.", marked=False)
    assert provider.calls[-1]["model"] == "test-before"
    messages = await native.runner._session_db.get_messages(native.old)
    assert any("This turn follows resume." in str(row.get("content")) for row in messages)
    assert not await native.runner._session_db.get_messages(native.current)


@pytest.mark.asyncio
async def test_native_model_pin_is_durable_and_next_real_turn_uses_it(native, provider):
    await dispatch(native, "/model --session --provider custom test-after")
    key = native.runner._session_key_for_source(native.source)
    entry = await native.runner.async_session_store.lookup_by_session_key(key)
    assert entry.model_override, native.adapter.sent
    assert entry.model_override["model"] == "test-after", native.adapter.sent
    assert entry.model_override["provider"] == "custom"
    persisted = durable_route(native)["model_override"]
    assert persisted == {"model": "test-after", "provider": "custom", "base_url": provider.url}
    await dispatch(native, "Check the selected model.", marked=False)
    assert provider.calls, native.adapter.sent
    assert provider.calls[-1]["model"] == "test-after"
    assert any("isolated provider reply" in text for text in native.adapter.sent)


@pytest.mark.asyncio
async def test_busy_native_turn_rejects_switch_without_interrupting(native, provider):
    provider.hold = lambda body: bool(body.get("messages"))
    turn = asyncio.create_task(dispatch(native, "Keep running.", marked=False))
    try:
        assert await asyncio.to_thread(provider.entered.wait, 15)
        await dispatch(native, f"/resume {native.old}")
        entry = await native.runner.async_session_store.lookup_by_session_key(
            native.runner._session_key_for_source(native.source)
        )
        assert entry.session_id == native.current
        assert not turn.done()
    finally:
        provider.release.set()
        await turn


@pytest.mark.asyncio
async def test_stale_menu_does_not_change_fresh_route_or_pin(native):
    key = native.runner._session_key_for_source(native.source)
    fresh = await native.runner.async_session_store.get_or_create_session(native.source, force_new=True)
    await dispatch(native, "/model --session --provider custom test-after")
    entry = await native.runner.async_session_store.lookup_by_session_key(key)
    assert entry.session_id == fresh.session_id
    assert entry.model_override is None


@pytest.mark.asyncio
async def test_resume_rejects_foreign_owner_through_native_store(native):
    source = native.adapter.build_source(chat_id="oc_foreign", chat_type="dm", user_id="other", user_name="Other")
    foreign = await native.runner.async_session_store.get_or_create_session(source)
    await dispatch(native, f"/resume {foreign.session_id}")
    entry = await native.runner.async_session_store.lookup_by_session_key(
        native.runner._session_key_for_source(native.source)
    )
    assert entry.session_id == native.current


@pytest.mark.asyncio
async def test_shared_runtime_admission_cannot_overtake_native_model_commit(native, provider):
    from gateway.platforms.event import MessageEvent

    provider.hold = lambda body: body.get("name") == "test-after"
    switch = asyncio.create_task(dispatch(native, "/model --session --provider custom test-after"))
    turn = None
    try:
        assert await asyncio.to_thread(provider.entered.wait, 15)
        # Bypass the adapter's chat lock just as another runtime ingress does,
        # but use the real callback wrapper, admission ledger and FIFO claim.
        event = MessageEvent(text="A concurrent runtime input.", source=native.source, message_id="concurrent")
        turn = asyncio.create_task(native.runner._primary_message_handler()(event))
        await asyncio.sleep(0.05)
        assert not turn.done()
        assert not any(body.get("messages") for body in provider.calls)
    finally:
        provider.release.set()
        await switch
        if turn:
            await asyncio.wait_for(turn, 20)
    assert provider.calls[-1]["model"] == "test-after"
    assert (
        native.runner.session_store.lookup_by_session_key(
            native.runner._session_key_for_source(native.source)
        ).model_override["model"]
        == "test-after"
    )


@pytest.mark.asyncio
async def test_new_menu_uses_native_reset_without_nested_lock_deadlock(native):
    await asyncio.wait_for(dispatch(native, "/new"), 10)
    await dispatch(native, "/approve", marked=False)
    entry = await native.runner.async_session_store.lookup_by_session_key(
        native.runner._session_key_for_source(native.source)
    )
    assert entry.session_id not in {native.current, native.old}, native.adapter.sent


@pytest.mark.asyncio
async def test_native_confirmation_rechecks_original_menu_session(native):
    await dispatch(native, "/new")
    newer = await native.runner.async_session_store.get_or_create_session(native.source, force_new=True)
    await dispatch(native, "/approve", marked=False)
    entry = await native.runner.async_session_store.lookup_by_session_key(
        native.runner._session_key_for_source(native.source)
    )
    assert entry.session_id == newer.session_id, native.adapter.sent


@pytest.mark.asyncio
async def test_cancelled_menu_holds_admission_fence_until_native_worker_finishes(native, provider):
    from gateway.platforms.event import MessageEvent

    provider.hold = lambda body: body.get("name") == "test-after"
    switch = asyncio.create_task(dispatch(native, "/model --session --provider custom test-after"))
    turn = None
    try:
        assert await asyncio.to_thread(provider.entered.wait, 15)
        switch.cancel()
        await asyncio.sleep(0.01)
        event = MessageEvent(text="Arrived during cancellation.", source=native.source, message_id="cancel-race")
        turn = asyncio.create_task(native.runner._primary_message_handler()(event))
        await asyncio.sleep(0.1)
        assert not any(body.get("messages") for body in provider.calls)
    finally:
        provider.release.set()
        await asyncio.gather(switch, return_exceptions=True)
        if turn:
            await asyncio.wait_for(turn, 20)
    assert provider.calls[-1]["model"] == "test-after"


@pytest.mark.asyncio
async def test_native_model_cost_confirmation_is_preserved_and_fenced(native):
    await dispatch(native, "/model --session --provider custom openai/gpt-5.5-pro")
    key = native.runner._session_key_for_source(native.source)
    entry = await native.runner.async_session_store.lookup_by_session_key(key)
    assert entry.model_override is None
    assert any("EXPENSIVE MODEL WARNING" in text for text in native.adapter.sent)
    await dispatch(native, "/approve", marked=False)
    entry = await native.runner.async_session_store.lookup_by_session_key(key)
    assert entry.model_override["model"] == "openai/gpt-5.5-pro", native.adapter.sent


@pytest.mark.asyncio
async def test_native_model_cost_confirmation_refuses_a_stale_route(native):
    await dispatch(native, "/model --session --provider custom openai/gpt-5.5-pro")
    newer = await native.runner.async_session_store.get_or_create_session(native.source, force_new=True)
    await dispatch(native, "/approve", marked=False)
    entry = await native.runner.async_session_store.lookup_by_session_key(
        native.runner._session_key_for_source(native.source)
    )
    assert entry.session_id == newer.session_id
    assert entry.model_override is None


@pytest.mark.asyncio
async def test_resume_rejects_target_owned_by_a_concurrent_native_claim(native):
    runner = native.runner
    key = runner._session_key_for_source(native.source)
    authority = runner.session_authority
    await runner.async_session_store.switch_session(key, native.old)
    old_ref = authority.register(native.source)
    await runner.async_session_store.switch_session(key, native.current)
    authority.register(native.source)
    live = authority.sessions[old_ref.session_id]
    assert live.route != key  # target is detached: source busy hint cannot hide this check
    async with live.claim_gate:
        assert not native.adapter.menu.host.busy(native.source)
        await dispatch(native, f"/resume {native.old}")
    assert durable_route(native)["session_id"] == native.current
