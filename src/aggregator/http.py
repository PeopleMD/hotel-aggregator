"""HTTP client that does not hammer other people's sites.

- at most one request per `interval` seconds to one host (per worker process);
- respects robots.txt when asked to;
- retries network errors, 429 and 5xx with exponential backoff and Retry-After;
- turns failures into two kinds of errors, so the worker knows whether a retry makes sense.
"""

import asyncio
import random
import time
from collections import defaultdict
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

MAX_RETRY_AFTER = 30.0


class TemporaryError(Exception):
    """Worth trying again later: timeout, 429, 5xx."""


class PermanentError(Exception):
    """Retrying will not help: 404, robots.txt, changed markup."""


class PoliteClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        interval: float = 5.0,
        attempts: int = 3,
        backoff: float = 1.0,
        host_intervals: dict[str, float] | None = None,
    ) -> None:
        self._client = client
        self._interval = interval
        self._host_intervals = host_intervals or {}
        self._attempts = attempts
        self._backoff = backoff
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._next_allowed: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser] = {}

    async def get(self, url: str, *, check_robots: bool = True, **kwargs: Any) -> httpx.Response:
        if check_robots:
            await self._check_robots(url)
        return await self._request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._request("POST", url, **kwargs)

    async def _wait_turn(self, host: str) -> None:
        async with self._locks[host]:
            delay = self._next_allowed.get(host, 0.0) - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            interval = self._host_intervals.get(host, self._interval)
            self._next_allowed[host] = time.monotonic() + interval

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        host = urlsplit(url).netloc
        reason = ""
        for attempt in range(self._attempts):
            await self._wait_turn(host)
            retry_after = 0.0
            try:
                response = await self._client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                reason = f"{type(exc).__name__}: {exc}"
            else:
                if response.is_success:
                    return response
                reason = f"HTTP {response.status_code}"
                if response.status_code != 429 and response.status_code < 500:
                    raise PermanentError(f"{reason} for {url}")
                retry_after = _parse_retry_after(response)
            if attempt + 1 < self._attempts:
                pause = self._backoff * 2**attempt + random.uniform(0, self._backoff)  # noqa: S311
                await asyncio.sleep(max(pause, retry_after))
        raise TemporaryError(f"{reason} for {url}")

    async def _check_robots(self, url: str) -> None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser = RobotFileParser()
            try:
                response = await self._request("GET", f"{origin}/robots.txt")
                parser.parse(response.text.splitlines())
            except PermanentError:
                # 4xx for robots.txt means "no restrictions" (RFC 9309).
                parser.parse([])
            self._robots[origin] = parser
        agent = self._client.headers.get("user-agent", "*")
        if not self._robots[origin].can_fetch(agent, url):
            raise PermanentError(f"robots.txt forbids {url}")


def _parse_retry_after(response: httpx.Response) -> float:
    value = response.headers.get("retry-after", "")
    return min(float(value), MAX_RETRY_AFTER) if value.isdigit() else 0.0
