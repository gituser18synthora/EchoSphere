"""Workflow checkpoints survive a dropped PostgreSQL connection.

Demo, 18 Nov 2026 15:02 IST: the database server ran out of disk,
PostgreSQL went through crash recovery and the pooler closed the gateway's
checkpoint connection. The engine held that ONE connection for the life of
the process, so every later workflow turn of every call failed with
``psycopg.OperationalError: the connection is closed`` until a restart.

The tests that talk to PostgreSQL run only against a database on this
machine (they terminate their own backend and write throw-away threads);
they are skipped everywhere else.
"""

import asyncio
import uuid
from urllib.parse import quote

import psycopg
import pytest

from shared.config import get_settings
from shared.orchestration import workflow_engine as wf_module
from shared.orchestration.workflow_engine import (
    WorkflowEngine,
    _open_postgres_checkpointer,
)

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _conninfo() -> str:
    settings = get_settings()
    user = quote(str(settings.postgres_user), safe="")
    password = quote(str(settings.postgres_password), safe="")
    return (
        f"postgresql://{user}:{password}"
        f"@{settings.postgres_host}:{settings.postgres_port}"
        f"/{settings.postgres_database}"
    )


def _local_postgres_available() -> bool:
    if str(get_settings().postgres_host) not in _LOCAL_HOSTS:
        return False
    try:
        with psycopg.connect(_conninfo(), connect_timeout=2):
            return True
    except Exception:  # noqa: BLE001 - any failure means "skip"
        return False


needs_local_postgres = pytest.mark.skipif(
    not _local_postgres_available(),
    reason="needs a PostgreSQL checkpoint database on this machine",
)


async def _pool_backend_pid(pool) -> int:
    async with pool.connection() as conn:
        row = await (await conn.execute("select pg_backend_pid() as pid")).fetchone()
        return int(row["pid"])


async def _terminate(pid: int) -> None:
    """Drop one server connection, as a crash or an admin kill would."""
    async with await psycopg.AsyncConnection.connect(
        _conninfo(), autocommit=True
    ) as admin:
        await admin.execute("select pg_terminate_backend(%s)", (pid,))
    await asyncio.sleep(0.3)


class TestPoolConfiguration:
    async def test_pool_checks_connections_and_fails_fast(self, monkeypatch):
        import langgraph.checkpoint.postgres.aio as saver_module
        import psycopg_pool

        seen: dict = {}

        class _Pool:
            check_connection = staticmethod(
                psycopg_pool.AsyncConnectionPool.check_connection
            )

            def __init__(self, conninfo, **kwargs):
                seen.update(kwargs, conninfo=conninfo)

            async def open(self, wait=False, timeout=None):
                seen["opened"] = (wait, timeout)

            async def close(self):
                seen["closed"] = True

        class _Saver:
            def __init__(self, conn):
                seen["saver_conn"] = conn

            async def setup(self):
                seen["setup"] = True

        monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", _Pool)
        monkeypatch.setattr(saver_module, "AsyncPostgresSaver", _Saver)

        pool, saver = await _open_postgres_checkpointer("postgresql://x/y")

        assert seen["saver_conn"] is pool and seen["setup"] is True
        # A connection is verified when a turn takes it: a dead one is
        # replaced instead of failing the turn.
        assert seen["check"] is psycopg_pool.AsyncConnectionPool.check_connection
        assert seen["open"] is False and seen["opened"][0] is True
        # Same connection options the single-connection saver used.
        assert seen["kwargs"]["autocommit"] is True
        assert seen["kwargs"]["prepare_threshold"] == 0
        # A database outage must fail the turn, not time the workflow out
        # (the turn timeout ends the workflow with done=True).
        assert seen["timeout"] < 10.0
        assert "closed" not in seen

    async def test_pool_is_closed_when_setup_fails(self, monkeypatch):
        import langgraph.checkpoint.postgres.aio as saver_module
        import psycopg_pool

        closed: list = []

        class _Pool:
            check_connection = None

            def __init__(self, conninfo, **kwargs):
                pass

            async def open(self, wait=False, timeout=None):
                pass

            async def close(self):
                closed.append(True)

        class _Saver:
            def __init__(self, conn):
                pass

            async def setup(self):
                raise psycopg.OperationalError("the database system is in recovery mode")

        monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", _Pool)
        monkeypatch.setattr(saver_module, "AsyncPostgresSaver", _Saver)

        with pytest.raises(psycopg.OperationalError):
            await _open_postgres_checkpointer("postgresql://x/y")
        assert closed == [True]

    async def test_engine_degrades_to_memory_when_postgres_is_down(self, monkeypatch):
        async def _down(conninfo):
            raise psycopg.OperationalError("connection refused")

        monkeypatch.setattr(wf_module, "_open_postgres_checkpointer", _down)
        engine = WorkflowEngine()
        saver = await engine._get_checkpointer()
        assert type(saver).__name__ in {"MemorySaver", "InMemorySaver"}
        assert engine._checkpoint_pool is None
        await engine.aclose()


@needs_local_postgres
class TestDroppedConnection:
    async def test_single_connection_never_recovers(self):
        """The old setup, kept as the reproduction of the Demo failure."""
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        config = {"configurable": {"thread_id": f"t-{uuid.uuid4().hex}", "checkpoint_ns": ""}}
        async with AsyncPostgresSaver.from_conn_string(_conninfo()) as saver:
            assert await saver.aget_tuple(config) is None
            row = await (await saver.conn.execute("select pg_backend_pid() as pid")).fetchone()
            await _terminate(int(row["pid"]))
            errors = []
            for _ in range(3):
                with pytest.raises(psycopg.OperationalError) as caught:
                    await saver.aget_tuple(config)
                errors.append(str(caught.value))
            assert errors[-1] == "the connection is closed"

    async def test_pooled_saver_reads_again_after_the_connection_is_dropped(self):
        config = {"configurable": {"thread_id": f"t-{uuid.uuid4().hex}", "checkpoint_ns": ""}}
        pool, saver = await _open_postgres_checkpointer(_conninfo())
        try:
            assert await saver.aget_tuple(config) is None
            first_pid = await _pool_backend_pid(pool)
            await _terminate(first_pid)
            for _ in range(3):
                assert await saver.aget_tuple(config) is None
            assert await _pool_backend_pid(pool) != first_pid
        finally:
            await pool.close()

    async def test_call_continues_after_the_connection_is_dropped(self):
        engine = WorkflowEngine()
        session = f"s-{uuid.uuid4().hex}"

        async def turn(text: str):
            return await engine.handle_turn(
                session_id=session, tenant_id="tn-x", bot_id="bot-x",
                workflow_name="appointment_booking", user_text=text,
            )

        try:
            reply, done = await turn("I want to book an appointment")
            assert "name" in reply.lower() and not done
            pool = engine._checkpoint_pool
            assert pool is not None, "engine fell back to the in-memory saver"

            await _terminate(await _pool_backend_pid(pool))

            # The next turn reads the checkpoint written before the drop
            # and carries on with the same booking.
            reply, done = await turn("My name is Asha Verma")
            assert "phone" in reply.lower() and not done

            await _terminate(await _pool_backend_pid(pool))
            reply, done = await turn("9876543210")
            assert "date" in reply.lower() and not done
        finally:
            saver = engine._checkpointer
            if engine._checkpoint_pool is not None and saver is not None:
                async with engine._checkpoint_pool.connection() as conn:
                    rows = await (await conn.execute(
                        "select distinct thread_id from checkpoints where thread_id like %s",
                        (f"{session}%",),
                    )).fetchall()
                for row in rows:
                    await saver.adelete_thread(row["thread_id"])
            await engine.aclose()

    async def test_cancelled_turn_leaves_the_connection_usable(self):
        """The workflow turn timeout cancels a running query; that must not
        cost the connection."""
        pool, saver = await _open_postgres_checkpointer(_conninfo())
        config = {"configurable": {"thread_id": f"t-{uuid.uuid4().hex}", "checkpoint_ns": ""}}

        async def _slow():
            async with pool.connection() as conn:
                await conn.execute("select pg_sleep(5)")

        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(_slow(), timeout=0.3)
            assert await saver.aget_tuple(config) is None
        finally:
            await pool.close()
