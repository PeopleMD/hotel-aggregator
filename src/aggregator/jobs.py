import logging
from datetime import UTC, datetime, timedelta

from aio_pika.abc import AbstractChannel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from aggregator import queue
from aggregator.config import get_settings
from aggregator.models import ACTIVE_STATUSES, ParseJob

log = logging.getLogger("jobs")


async def create_job(
    session: AsyncSession, channel: AbstractChannel, city: str, source: str
) -> ParseJob | None:
    """None if an unfinished job for this city and source already exists.

    Commit goes before publish, otherwise a fast worker could receive an id that is not
    in the database yet. If we die in between, `requeue_stale` publishes the job later.
    """
    stmt = insert(ParseJob).values(city=city, source=source).on_conflict_do_nothing()
    job = await session.scalar(stmt.returning(ParseJob))
    await session.commit()
    if job is None:
        return None
    try:
        await queue.publish_job(channel, source, job.id)
    except Exception:
        log.exception("job %s is saved but not published, the scheduler will retry", job.id)
    return job


async def requeue_stale(session: AsyncSession, channel: AbstractChannel) -> int:
    """Publish again the jobs that got lost between the database and the queue.

    An extra copy is harmless: the worker skips finished jobs and saving is idempotent.
    """
    border = datetime.now(UTC) - timedelta(minutes=get_settings().stale_job_minutes)
    stale = list(
        await session.scalars(
            select(ParseJob).where(
                ParseJob.status.in_(ACTIVE_STATUSES), ParseJob.updated_at < border
            )
        )
    )
    for job in stale:
        await queue.publish_job(channel, job.source, job.id)
        job.updated_at = datetime.now(UTC)
    await session.commit()
    return len(stale)
