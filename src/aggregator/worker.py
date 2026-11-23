"""Takes jobs from RabbitMQ, runs a parser, saves the result.

Delivery is at-least-once: the message is acked only after the commit. A worker killed in
the middle leaves the message unacked and RabbitMQ gives it to another worker. That is
safe because saving is idempotent (upsert by source + external_id).
"""

import asyncio
import json
import logging
import signal
from urllib.parse import urlsplit

import httpx
from aio_pika.abc import AbstractChannel, AbstractIncomingMessage
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from aggregator import queue
from aggregator.cities import CITIES
from aggregator.config import get_settings
from aggregator.db import get_sessionmaker
from aggregator.http import PermanentError, PoliteClient, TemporaryError
from aggregator.models import ParseJob
from aggregator.parsers import SOURCES
from aggregator.storage import save_offers

log = logging.getLogger("worker")


def is_infrastructure_error(exc: Exception) -> bool:
    if isinstance(exc, OSError | OperationalError | InterfaceError):
        return True
    return isinstance(exc, DBAPIError) and exc.connection_invalidated


class Worker:
    def __init__(self, channel: AbstractChannel, client: PoliteClient) -> None:
        self._channel = channel
        self._client = client

    async def handle(self, message: AbstractIncomingMessage) -> None:
        try:
            job_id = int(json.loads(message.body)["job_id"])
        except (ValueError, KeyError, TypeError):
            log.error("malformed message, moved to dead queue: %r", message.body[:200])
            await message.reject(requeue=False)
            return

        try:
            await self._run(message, job_id)
        except Exception as exc:
            log.exception("job %s: unexpected error", job_id)
            if is_infrastructure_error(exc):
                # The database is unavailable, the job itself is fine: give it back.
                await asyncio.sleep(get_settings().requeue_pause)
                await message.reject(requeue=True)
            else:
                # Broken data or a bug: requeue would loop forever.
                await self._set_status(job_id, "failed", f"{type(exc).__name__}: {exc}")
                await message.reject(requeue=False)

    async def _run(self, message: AbstractIncomingMessage, job_id: int) -> None:
        async with get_sessionmaker()() as session:
            job = await session.get(ParseJob, job_id)
            if job is None or job.status in ("done", "failed"):
                log.info("job %s: nothing to do (duplicate delivery?)", job_id)
                await message.ack()
                return
            job.status = "running"
            job.attempts += 1
            attempt, city, source = job.attempts, job.city, job.source
            await session.commit()

        try:
            # No transaction is open while we wait for a slow site.
            offers = await SOURCES[source](CITIES[city], self._client)
            if not offers:
                raise PermanentError("source returned nothing, markup changed?")
        except TemporaryError as exc:
            await self._retry_or_fail(message, job_id, source, attempt, str(exc))
            return
        except Exception as exc:
            log.exception("job %s failed", job_id)
            await self._set_status(job_id, "failed", f"{type(exc).__name__}: {exc}")
            await message.reject(requeue=False)
            return

        async with get_sessionmaker()() as session, session.begin():
            stats = await save_offers(session, city, source, offers)
            job = await session.get_one(ParseJob, job_id)
            job.status, job.error, job.stats = "done", None, stats.as_dict()
        await message.ack()
        log.info("job %s (%s/%s) done: %s", job_id, city, source, stats)

    async def _retry_or_fail(
        self,
        message: AbstractIncomingMessage,
        job_id: int,
        source: str,
        attempt: int,
        error: str,
    ) -> None:
        delays = get_settings().retry_delays
        if attempt > len(delays):
            log.error("job %s: attempts are over: %s", job_id, error)
            await self._set_status(job_id, "failed", error)
            await message.reject(requeue=False)
            return
        delay = delays[attempt - 1]
        log.warning("job %s: attempt %s failed, retry in %ss: %s", job_id, attempt, delay, error)
        await self._set_status(job_id, "retrying", error)
        # Publish first, ack second: a crash in between gives a duplicate, not a loss.
        await queue.publish(self._channel, queue.retry_queue(source, delay), {"job_id": job_id})
        await message.ack()

    async def _set_status(self, job_id: int, status: str, error: str) -> None:
        async with get_sessionmaker()() as session, session.begin():
            job = await session.get_one(ParseJob, job_id)
            job.status, job.error = status, error[:2000]


def build_client(client: httpx.AsyncClient) -> PoliteClient:
    settings = get_settings()
    return PoliteClient(
        client,
        interval=settings.host_interval,
        attempts=settings.http_attempts,
        backoff=settings.http_backoff,
        host_intervals={urlsplit(settings.demo_supplier_url).netloc: settings.demo_interval},
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = get_settings()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    headers = {"User-Agent": settings.user_agent}
    async with (
        httpx.AsyncClient(
            headers=headers, timeout=settings.http_timeout, follow_redirects=True
        ) as http,
        await queue.connect() as connection,
    ):
        channel = await connection.channel()
        # One unacked message per consumer: a source is parsed one city at a time.
        await channel.set_qos(prefetch_count=1)
        worker = Worker(channel, build_client(http))
        for tasks in (await queue.declare(channel)).values():
            await tasks.consume(worker.handle)
        log.info("worker started")
        # Polling, because signal.signal (the only option on Windows) cannot wake the loop.
        while not stop.is_set():  # noqa: ASYNC110
            await asyncio.sleep(0.5)
        log.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
