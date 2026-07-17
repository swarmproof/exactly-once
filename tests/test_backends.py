"""Redis & Postgres adapter tests — the same guarantees, on the distributed stores.

Gated on Docker (via testcontainers). Skipped where Docker is unavailable; the CI
matrix runs them. They mirror the store-contract and the core safety properties
(one execution under a thread race; exception leaves IN_FLIGHT) on the real backends.
"""

from __future__ import annotations

import contextlib
import subprocess
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

from exactly_once import QuarantinedError, State, Store, once


def _docker_available() -> bool:
    try:
        subprocess.run(
            ["docker", "info"], capture_output=True, timeout=5, check=True
        )
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _docker_available(), reason="Docker not available")


# --- Redis -----------------------------------------------------------------


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7") as rc:
        host = rc.get_container_host_ip()
        port = rc.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"


@pytest.fixture
def redis_store(redis_url: str) -> Iterator[Store]:
    store = Store.redis(redis_url)
    store._client.flushdb()  # type: ignore[attr-defined]
    yield store
    store.close()


@pytest.mark.redis
def test_redis_contract(redis_store: Store) -> None:
    assert redis_store.claim("k").state is State.FRESH
    assert redis_store.claim("k").state is State.IN_FLIGHT
    redis_store.commit("k", b"payload")
    r = redis_store.claim("k")
    assert r.state is State.COMMITTED and r.result == b"payload"
    redis_store.commit("k", b"other")  # idempotent
    assert redis_store.claim("k").result == b"payload"


@pytest.mark.redis
def test_redis_release_and_ledger(redis_store: Store) -> None:
    redis_store.claim("a")
    redis_store.claim("b")
    redis_store.commit("b", b"x")
    assert {r.key for r in redis_store.list(State.IN_FLIGHT)} == {"a"}
    redis_store.release("a")
    assert redis_store.get("a") is None


@pytest.mark.redis
def test_redis_thread_race_one_execution(redis_store: Store) -> None:
    n = {"c": 0}
    lock = threading.Lock()
    barrier = threading.Barrier(24)

    @once(redis_store, key="race:1")
    def effect() -> str:
        with lock:
            n["c"] += 1
        return "ok"

    def worker(_: int) -> None:
        barrier.wait()
        with contextlib.suppress(QuarantinedError):
            effect()

    with ThreadPoolExecutor(max_workers=24) as ex:
        list(ex.map(worker, range(24)))
    assert n["c"] == 1


@pytest.mark.redis
def test_redis_exception_leaves_in_flight(redis_store: Store) -> None:
    @once(redis_store, key="risky")
    def risky() -> str:
        raise ValueError("boom")

    with pytest.raises(ValueError):
        risky()
    assert redis_store.get("risky").state is State.IN_FLIGHT


@pytest.mark.redis
async def test_redis_async(redis_store: Store) -> None:
    n = {"c": 0}

    @once(redis_store, key="charge:async")
    async def charge() -> str:
        n["c"] += 1
        return "ok"

    assert await charge() == "ok"
    assert await charge() == "ok"  # replay via native async client
    assert n["c"] == 1
    assert (await redis_store.aget("charge:async")).state is State.COMMITTED
    await redis_store.aclose()


# --- Postgres --------------------------------------------------------------


@pytest.fixture(scope="module")
def pg_dsn() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16", driver=None) as pg:
        yield pg.get_connection_url()


@pytest.fixture
def pg_store(pg_dsn: str) -> Iterator[Store]:
    store = Store.postgres(pg_dsn)
    with store._conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute("TRUNCATE effects")
    yield store
    store.close()


@pytest.mark.postgres
def test_pg_contract(pg_store: Store) -> None:
    assert pg_store.claim("k").state is State.FRESH
    assert pg_store.claim("k").state is State.IN_FLIGHT
    pg_store.commit("k", b"payload")
    r = pg_store.claim("k")
    assert r.state is State.COMMITTED and r.result == b"payload"


@pytest.mark.postgres
def test_pg_thread_race_one_execution(pg_store: Store) -> None:
    n = {"c": 0}
    lock = threading.Lock()
    barrier = threading.Barrier(24)

    @once(pg_store, key="race:1")
    def effect() -> str:
        with lock:
            n["c"] += 1
        return "ok"

    def worker(_: int) -> None:
        barrier.wait()
        with contextlib.suppress(QuarantinedError):
            effect()

    with ThreadPoolExecutor(max_workers=24) as ex:
        list(ex.map(worker, range(24)))
    assert n["c"] == 1


@pytest.mark.postgres
def test_pg_exception_leaves_in_flight(pg_store: Store) -> None:
    @once(pg_store, key="risky")
    def risky() -> str:
        raise ValueError("boom")

    with pytest.raises(ValueError):
        risky()
    assert pg_store.get("risky").state is State.IN_FLIGHT


@pytest.mark.postgres
async def test_pg_async(pg_store: Store) -> None:
    n = {"c": 0}

    @once(pg_store, key="charge:async")
    async def charge() -> str:
        n["c"] += 1
        return "ok"

    assert await charge() == "ok"
    assert await charge() == "ok"  # replay via native async connection
    assert n["c"] == 1
    await pg_store.arelease("nonexistent")  # no-op path
    await pg_store.aclose()
