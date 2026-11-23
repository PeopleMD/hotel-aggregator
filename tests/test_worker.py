import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError

from aggregator import queue, worker
from aggregator.db import get_sessionmaker
from aggregator.http import PermanentError, TemporaryError
from aggregator.jobs import create_job, requeue_stale
from aggregator.models import Hotel, ParseJob
from aggregator.parsers import SOURCES, RawOffer

OFFER = RawOffer(
    external_id="1", name="Inn", url="http://x/", lat=47, lon=28, price=9, currency="EUR"
)


@pytest.fixture
def source(monkeypatch):
    """Replaces the demo parser: each call takes the next result from the list."""
    results, calls = [], []

    async def fetch(city, client):
        calls.append(city.slug)
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setitem(SOURCES, "demo", fetch)
    fetch.results, fetch.calls = results, calls
    return fetch


@pytest.fixture
async def running(session, channel):
    tasks = await channel.declare_queue(queue.tasks_queue("demo"), passive=True)
    tag = await tasks.consume(worker.Worker(channel, None).handle)
    yield
    await tasks.cancel(tag)


async def wait_status(job_id, *statuses):
    for _ in range(100):
        async with get_sessionmaker()() as fresh:
            job = await fresh.get_one(ParseJob, job_id)
        if job.status in statuses:
            return job
        await asyncio.sleep(0.1)
    raise AssertionError(f"job is {job.status}, expected {statuses}")


async def dead_letters(channel):
    await asyncio.sleep(0.3)
    dead = await channel.get_queue(queue.dead_queue())
    count = 0
    while await dead.get(fail=False, no_ack=True) is not None:
        count += 1
    return count


async def test_success(session, channel, source, running):
    source.results.append([OFFER])
    job = await create_job(session, channel, "chisinau", "demo")
    job = await wait_status(job.id, "done")
    assert job.stats["new_hotels"] == 1
    assert await dead_letters(channel) == 0


async def test_temporary_error_is_retried_after_delay(session, channel, source, running):
    source.results += [TemporaryError("HTTP 503"), [OFFER]]
    started = asyncio.get_running_loop().time()
    job = await create_job(session, channel, "chisinau", "demo")
    job = await wait_status(job.id, "done")
    assert job.attempts == 2
    assert job.error is None
    assert asyncio.get_running_loop().time() - started >= 1


async def test_dead_letter_after_last_attempt(session, channel, source, running):
    source.results += [TemporaryError("HTTP 503"), TemporaryError("HTTP 503 again")]
    job = await create_job(session, channel, "chisinau", "demo")
    job = await wait_status(job.id, "failed")
    assert (job.attempts, job.error) == (2, "HTTP 503 again")
    assert await dead_letters(channel) == 1


@pytest.mark.parametrize("result", [PermanentError("robots"), KeyError("bug in parser"), []])
async def test_permanent_error_is_not_retried(session, channel, source, running, result):
    source.results.append(result)
    job = await create_job(session, channel, "chisinau", "demo")
    job = await wait_status(job.id, "failed")
    assert job.attempts == 1
    assert await dead_letters(channel) == 1


async def test_broken_data_fails_instead_of_looping(session, channel, source, running):
    source.results.append([OFFER.model_copy(update={"price": -1})])  # violates a DB constraint
    job = await create_job(session, channel, "chisinau", "demo")
    job = await wait_status(job.id, "failed")
    assert "IntegrityError" in job.error
    assert await dead_letters(channel) == 1


async def test_database_outage_requeues(session, channel, source, running, monkeypatch):
    source.results += [[OFFER], [OFFER]]
    real_save, calls = worker.save_offers, []

    async def flaky_save(*args):
        calls.append(1)
        if len(calls) == 1:
            raise OperationalError("stmt", {}, Exception("connection lost"))
        return await real_save(*args)

    monkeypatch.setattr(worker, "save_offers", flaky_save)
    job = await create_job(session, channel, "chisinau", "demo")
    await wait_status(job.id, "done")
    assert len(calls) == 2


async def test_malformed_message_goes_to_dead_queue(session, channel, running):
    await queue.publish(channel, queue.tasks_queue("demo"), {"wrong": True})
    assert await dead_letters(channel) == 1


async def test_duplicate_delivery_is_ignored(session, channel, source, running):
    source.results.append([OFFER])
    job = await create_job(session, channel, "chisinau", "demo")
    await wait_status(job.id, "done")
    await queue.publish_job(channel, "demo", job.id)
    await asyncio.sleep(0.5)
    assert len(source.calls) == 1
    assert await session.scalar(select(func.count()).select_from(Hotel)) == 1


async def test_only_one_unfinished_job(session, channel):
    assert await create_job(session, channel, "chisinau", "demo") is not None
    assert await create_job(session, channel, "chisinau", "demo") is None
    assert await create_job(session, channel, "lviv", "demo") is not None


async def test_only_one_worker_serves_a_source(session, channel, source):
    """Single active consumer: two workers, but never two parallel requests to one site."""
    active, peak = 0, 0

    async def slow(city, client):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.3)
        active -= 1
        return [OFFER]

    SOURCES["demo"] = slow  # restored by the `source` fixture
    async with await queue.connect() as other:
        tags = []
        for ch in (channel, await other.channel()):
            tasks = await ch.declare_queue(queue.tasks_queue("demo"), passive=True)
            tags.append((tasks, await tasks.consume(worker.Worker(ch, None).handle)))
            await ch.set_qos(prefetch_count=1)
        jobs = [await create_job(session, channel, city, "demo") for city in ("chisinau", "lviv")]
        for job in jobs:
            await wait_status(job.id, "done", "failed")
        for tasks, tag in tags:
            await tasks.cancel(tag)
    assert peak == 1


async def test_lost_job_is_published_again(session, channel, source, running):
    source.results.append([OFFER])
    job = ParseJob(city="chisinau", source="demo")
    session.add(job)
    await session.commit()
    assert await requeue_stale(session, channel) == 0  # still fresh
    old = datetime.now(UTC) - timedelta(hours=1)
    await session.execute(update(ParseJob).values(updated_at=old))
    await session.commit()
    assert await requeue_stale(session, channel) == 1
    await wait_status(job.id, "done")
