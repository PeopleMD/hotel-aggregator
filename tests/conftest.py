"""Integration tests use real PostgreSQL and RabbitMQ: TEST_DATABASE_URL and TEST_RABBIT_URL."""

import os
import uuid
from pathlib import Path

import pytest

os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://app:app@localhost:5432/hotels_test"
)
os.environ["RABBIT_URL"] = os.environ.get("TEST_RABBIT_URL", "amqp://guest:guest@localhost:5672/")
os.environ["JWT_SECRET"] = "test-secret-" + "x" * 32
os.environ["RETRY_DELAYS"] = "[1]"
os.environ["REQUEUE_PAUSE"] = "0"
os.environ["CONTACT_URL"] = ""
os.environ["QUEUE_PREFIX"] = f"test-{uuid.uuid4().hex[:8]}"

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import text  # noqa: E402

from aggregator import queue  # noqa: E402
from aggregator.config import get_settings  # noqa: E402
from aggregator.db import get_sessionmaker  # noqa: E402
from aggregator.models import Base  # noqa: E402
from aggregator.parsers import SOURCES  # noqa: E402

ROOT = Path(__file__).parent.parent

# The tests wipe every table, so refuse to run on anything but a test database.
assert get_settings().database_url.endswith("_test"), "database name must end with _test"


@pytest.fixture(scope="session")
def migrated():
    config = Config(ROOT / "alembic.ini")
    command.upgrade(config, "head")
    command.check(config)


@pytest.fixture
async def session(migrated):
    async with get_sessionmaker()() as session:
        tables = ", ".join(Base.metadata.tables)
        await session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        await session.commit()
        yield session


@pytest.fixture
async def channel():
    async with await queue.connect() as connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=1)
        await queue.declare(channel)
        yield channel
        names = [queue.dead_queue()]
        for source in SOURCES:
            names.append(queue.tasks_queue(source))
            names += [queue.retry_queue(source, d) for d in get_settings().retry_delays]
        for name in names:
            await channel.queue_delete(name)


@pytest.fixture
def data():
    return Path(__file__).parent / "data"
