"""Hermes 0.23.2 compatibility: native command fences and read-only previews.

No public adapter session-list/history or expected-session command API exists yet.
Keep the version-sensitive owner/claim compatibility surface here; fail closed if
it is unavailable. Native commands still own authorization and all persistence.
"""

from __future__ import annotations

import asyncio
import json
import shlex
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

MENU_COMMAND: ContextVar[tuple[Any, str] | None] = ContextVar("lark_menu_command", default=None)


async def finish_menu_command(awaitable: Any) -> Any:
    """Keep the owner's fences until off-loop native work AND publication end."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        # Retrieve a failure too, so no task exception is left unobserved.
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()


class MenuHost:
    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter

    @property
    def runner(self) -> Any:
        runner = getattr(self.adapter, "gateway_runner", None)
        if runner is None:
            raise RuntimeError("Hermes gateway runner is unavailable")
        if getattr(getattr(runner, "config", None), "multiplex_profiles", False):
            raise RuntimeError("Menu session reads do not support multiplex profiles yet")
        return runner

    def key(self, source: Any) -> str:
        canonicalize = getattr(self.adapter, "_canonicalize", None)
        if canonicalize:
            canonicalize(source)
            if getattr(source, "profile_route_rejected", False):
                raise PermissionError("Profile route rejected")
        return str(self.runner._session_key_for_source(source))

    async def current(self, source: Any) -> str | None:
        key = self.key(source)
        entry = await self.runner.async_session_store.lookup_by_session_key(key)
        return str(entry.session_id) if entry else ""

    def busy(self, source: Any) -> bool:
        key = self.key(source)
        runner = self.runner
        if key in self.adapter._active_sessions or key in getattr(runner, "_running_agents", {}):
            return True
        authority = getattr(runner, "session_authority", None)
        if authority is None:
            return False
        # This synchronous UI hint does not touch SQLite. The commit guard below
        # additionally checks durable queued/unknown work while holding owner locks.
        for sid, live in authority.sessions.items():
            if live.route == key and (
                sid in authority.executing or live.claim_gate.locked() or live.mutation_lock.locked()
            ):
                return True
        return False

    @asynccontextmanager
    async def command_guard(
        self,
        event: Any,
        expected: str,
        *,
        confirmation: bool = False,
    ) -> AsyncIterator[None]:
        """Fence a native menu command, not the task that merely schedules it.

        Hermes 0.23.2's claim gate excludes FIFO starts. The owner's mutation
        lock excludes policy/route publication and admissions. Keep both through
        native /model or /resume dispatch, without replacing the native handler,
        authorization, argument parser, selection guards or persistence.
        /new takes mutation_lock itself, so only its claim gate is retained.
        """
        source = event.source
        key = self.key(source)
        runner = self.runner
        own_confirmation = confirmation and self.adapter._session_tasks.get(key) is asyncio.current_task()
        busy = self.busy(source) if not own_confirmation else key in getattr(runner, "_running_agents", {})
        if busy or await self.current(source) != expected:
            raise PermissionError("Menu session is busy or stale")
        command = event.get_command()
        if command not in {"model", "resume", "new"}:
            raise PermissionError("Unsupported menu command")
        target = None
        if command == "resume":
            parts = shlex.split(event.get_command_args())
            if len(parts) != 1 or not await self._allowed(source, parts[0]):
                raise PermissionError("Menu resume target is not owned by this caller")
            target = parts[0]
        authority = getattr(runner, "session_authority", None)
        async with AsyncExitStack() as stack:
            if authority is not None:
                from gateway.session_authority import LiveSession  # type: ignore[import-untyped]
                from hermes_state_runtime import list_session_admissions  # type: ignore[import-untyped]

                # setdefault is also the host's route-rotation convention: cold
                # mounts must expose the SAME locks to a concurrently arriving turn.
                owners = sorted({authority.logical_owner(sid) for sid in (expected, target) if sid})
                lives = [(sid, authority.sessions.setdefault(sid, LiveSession(source, key))) for sid in owners]
                for _, live in lives:
                    if live.claim_gate.locked() or live.mutation_lock.locked():
                        raise PermissionError("Session admission is in progress")
                for _, live in lives:
                    await stack.enter_async_context(live.claim_gate)
                mutations = await stack.enter_async_context(AsyncExitStack())
                for _, live in lives:
                    await mutations.enter_async_context(live.mutation_lock)
                for sid, live in lives:
                    pending = await asyncio.to_thread(list_session_admissions, authority.db, session_id=sid)
                    if pending or sid in authority.executing or runner._is_session_running(live.route):
                        raise PermissionError("Session has pending or running work")
                # /new's native handler owns its mutation critical section.
                # Drop just these inner locks, keeping the outer claim gates.
                if command == "new":
                    await mutations.aclose()
            if await self.current(source) != expected or key in getattr(runner, "_running_agents", {}):
                raise PermissionError("Menu session changed during admission")
            if target and not await self._allowed(source, target):
                raise PermissionError("Menu resume target owner changed")
            token = MENU_COMMAND.set((event, expected))
            try:
                yield
            finally:
                MENU_COMMAND.reset(token)

    async def recent_dm_sources(self, limit: int = 20) -> list[Any]:
        """Recent Feishu DM sources from state.db, as routing hints only.

        The owner mapping lives in memory; after a gateway restart it is empty and a
        menu click would otherwise be dropped. ``owner`` restored from here is still
        re-authorized per click (pairing/admission + sender authorization).
        """
        if getattr(getattr(self.runner, "config", None), "multiplex_profiles", False):
            raise RuntimeError("Menu session reads do not support multiplex profiles yet")
        from gateway.config import Platform  # type: ignore[import-untyped]
        from gateway.session import SessionSource  # type: ignore[import-untyped]

        from hermes_constants import get_hermes_home  # type: ignore[import-untyped]
        from hermes_state import SessionDB  # type: ignore[import-untyped]

        # Read the same store the gateway writes, but through an independent
        # read-only handle: the runner's store facade does not expose list queries.
        db = await asyncio.to_thread(SessionDB, Path(get_hermes_home()) / "state.db", read_only=True)
        try:
            rows = await asyncio.to_thread(
                db.list_sessions_rich,
                source="feishu",
                limit=limit,
                order_by_last_active=True,
                compact_rows=True,
            )
        finally:
            await asyncio.to_thread(db.close)
        sources: list[Any] = []
        for row in rows:
            origin = row.get("origin_json") or {}
            if isinstance(origin, str):
                try:
                    origin = json.loads(origin)
                except ValueError:
                    continue
            if not isinstance(origin, dict) or origin.get("chat_type") != "dm":
                continue
            chat_id, user_id = origin.get("chat_id"), origin.get("user_id")
            if not chat_id or not user_id:
                continue
            sources.append(
                SessionSource(
                    platform=Platform.FEISHU,
                    chat_id=chat_id,
                    chat_name=origin.get("chat_name"),
                    chat_type="dm",
                    user_id=user_id,
                    user_name=origin.get("user_name"),
                    user_id_alt=origin.get("user_id_alt"),
                )
            )
        return sources

    def _reader(self, source: Any) -> Any:
        from hermes_state import SessionDB  # type: ignore[import-untyped]

        home = self.runner._resolve_profile_home_for_source(source)
        return SessionDB(Path(home) / "state.db", read_only=True)

    async def _allowed(self, source: Any, sid: str) -> bool:
        verify = getattr(self.runner, "_resume_target_allowed", None)
        return bool(verify and await verify(source, sid, allow_override=False))

    async def sessions(self, source: Any, page: int) -> tuple[list[dict[str, Any]], bool]:
        from .bot_menu import PAGE_SIZE

        key = self.key(source)
        db = await asyncio.to_thread(self._reader, source)
        try:
            # Scope in the storage query, BEFORE any rows/previews reach this plugin.
            # Use compact rows; titles/ids are the only fields the menu renders.
            rows = await asyncio.to_thread(
                db.list_sessions_rich,
                source=source.platform.value,
                session_key=key,
                limit=PAGE_SIZE + 1,
                offset=page * PAGE_SIZE,
                order_by_last_active=True,
                compact_rows=True,
            )
            more = len(rows) > PAGE_SIZE
            rows = [row for row in rows[:PAGE_SIZE] if await self._allowed(source, row["id"])]
            return rows, more
        finally:
            await asyncio.to_thread(db.close)

    async def history(self, source: Any, sid: str, page: int) -> tuple[list[tuple[str, str]], bool]:
        from .bot_menu import PAGE_SIZE, plain_messages

        self.key(source)
        if not await self._allowed(source, sid):
            raise PermissionError("Session owner could not be verified")
        db = await asyncio.to_thread(self._reader, source)
        try:
            # Match the actual resume display walker (not the first-child
            # compression tree, which can omit a second compression child).
            lineage = await asyncio.to_thread(db._resume_lineage_ids, sid)
            if sid not in lineage:
                raise PermissionError("Missing compression lineage")
            for ancestor in lineage:
                if not await self._allowed(source, ancestor):
                    raise PermissionError("Compacted ancestor owner could not be verified")
            # Never expand ancestors again after verifying them: a concurrent
            # reparent/rotation must not add an unverified segment to this read.
            rows = await asyncio.to_thread(self._history_rows, db, lineage)
            visible = [
                (role, text[i : i + 1800]) for role, text in plain_messages(rows) for i in range(0, len(text), 1800)
            ]
            end = (page + 1) * PAGE_SIZE
            return visible[page * PAGE_SIZE : end], len(visible) > end
        finally:
            await asyncio.to_thread(db.close)

    @staticmethod
    def _history_rows(db: Any, lineage: list[str]) -> list[dict[str, Any]]:
        # The same raw display-set/dedup/decoder pipeline as native resume,
        # but with the already-verified segment list, never a second tree walk.
        rows = db._fetch_conversation_rows(lineage, "", with_compacted=True, with_session_id=True)
        rows = db._dedupe_display_generations(rows, drop_removed=True)
        return [db._row_to_message_dict(row, warn_context="lark_menu", summary_flag=True) for row in rows]
