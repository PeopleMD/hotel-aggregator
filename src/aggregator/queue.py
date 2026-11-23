"""RabbitMQ topology, one set of queues per source.

    parse.tasks.osm ──error, can retry──> parse.retry.osm.<delay> ──TTL──> parse.tasks.osm
           └─────────rejected───────────> parse.dead

Why per source: task queues have a single active consumer, so however many workers run,
only one of them talks to a given site. Other workers serve other sources and take over
if the active one dies. Otherwise N workers would break the per-host rate limit N times.

Why one retry queue per delay: with a per-message TTL in a single queue a long delay at
the head would block shorter ones behind it.
"""

import json
from typing import Any

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractQueue, AbstractRobustConnection

from aggregator.config import get_settings
from aggregator.parsers import SOURCES


async def connect() -> AbstractRobustConnection:
    return await aio_pika.connect_robust(get_settings().rabbit_url)


def tasks_queue(source: str) -> str:
    return f"{get_settings().queue_prefix}.tasks.{source}"


def retry_queue(source: str, delay: int) -> str:
    return f"{get_settings().queue_prefix}.retry.{source}.{delay}"


def dead_queue() -> str:
    return f"{get_settings().queue_prefix}.dead"


async def declare(channel: AbstractChannel) -> dict[str, AbstractQueue]:
    await channel.declare_queue(dead_queue(), durable=True)
    tasks = {}
    for source in SOURCES:
        tasks[source] = await channel.declare_queue(
            tasks_queue(source),
            durable=True,
            arguments={
                "x-single-active-consumer": True,
                "x-dead-letter-exchange": "",
                "x-dead-letter-routing-key": dead_queue(),
            },
        )
        for delay in get_settings().retry_delays:
            await channel.declare_queue(
                retry_queue(source, delay),
                durable=True,
                arguments={
                    "x-message-ttl": delay * 1000,
                    "x-dead-letter-exchange": "",
                    "x-dead-letter-routing-key": tasks_queue(source),
                },
            )
    return tasks


async def publish(channel: AbstractChannel, queue_name: str, body: dict[str, Any]) -> None:
    """Persistent message; aio-pika waits for the broker's confirm."""
    await channel.default_exchange.publish(
        aio_pika.Message(
            json.dumps(body).encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        ),
        routing_key=queue_name,
        mandatory=True,
        timeout=10,
    )


async def publish_job(channel: AbstractChannel, source: str, job_id: int) -> None:
    await publish(channel, tasks_queue(source), {"job_id": job_id})
