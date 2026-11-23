"""Creates jobs for every city and source on a timer and rescues stale ones."""

import asyncio
import logging
import time

from aggregator import queue
from aggregator.cities import CITIES
from aggregator.config import get_settings
from aggregator.db import get_sessionmaker
from aggregator.jobs import create_job, requeue_stale
from aggregator.parsers import enabled_sources

log = logging.getLogger("scheduler")
TICK_SECONDS = 60


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    interval = get_settings().scheduler_interval_hours * 3600
    next_run = time.monotonic()
    async with await queue.connect() as connection:
        channel = await connection.channel()
        await queue.declare(channel)
        while True:
            try:
                async with get_sessionmaker()() as session:
                    if rescued := await requeue_stale(session, channel):
                        log.warning("requeued %s stale jobs", rescued)
                    if time.monotonic() >= next_run:
                        next_run = time.monotonic() + interval
                        for city in CITIES:
                            for source in enabled_sources():
                                await create_job(session, channel, city, source)
                        log.info("jobs created")
            except Exception:
                log.exception("tick failed")
            await asyncio.sleep(TICK_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
