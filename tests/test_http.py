import asyncio
import time

import httpx
import pytest

from aggregator.http import PermanentError, PoliteClient, TemporaryError


def make_client(handler, **kwargs):
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers={"User-Agent": "testbot"}
    )
    return PoliteClient(http, **{"interval": 0, "backoff": 0, **kwargs})


def sequence(*statuses):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(statuses[len(calls) - 1], text="body")

    return handler, calls


async def test_retries_then_succeeds():
    handler, calls = sequence(503, 429, 200)
    response = await make_client(handler).post("http://a/")
    assert response.text == "body"
    assert len(calls) == 3


async def test_gives_up_with_temporary_error():
    handler, calls = sequence(500, 500, 500)
    with pytest.raises(TemporaryError, match="500"):
        await make_client(handler).post("http://a/")
    assert len(calls) == 3


async def test_network_error_is_temporary():
    def handler(request):
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(TemporaryError, match="ConnectTimeout"):
        await make_client(handler).post("http://a/")


async def test_404_is_permanent_and_not_retried():
    handler, calls = sequence(404)
    with pytest.raises(PermanentError):
        await make_client(handler).post("http://a/")
    assert len(calls) == 1


async def test_retry_after_is_respected():
    calls = []

    def handler(request):
        calls.append(time.monotonic())
        headers = {"Retry-After": "1"}
        return httpx.Response(429, headers=headers) if len(calls) == 1 else httpx.Response(200)

    await make_client(handler).post("http://a/")
    assert calls[1] - calls[0] >= 0.95


async def test_rate_limit_is_per_host():
    times = {"a": [], "b": []}

    def handler(request):
        times[request.url.host].append(time.monotonic())
        return httpx.Response(200)

    client = make_client(handler, interval=0.2)
    await asyncio.gather(*(client.post(f"http://{h}/") for h in "aaab"))
    gaps = [later - earlier for earlier, later in zip(times["a"], times["a"][1:], strict=False)]
    assert len(gaps) == 2
    assert min(gaps) >= 0.18
    assert times["b"][0] - times["a"][0] < 0.15  # other hosts do not wait


async def test_robots_txt():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/")
        return httpx.Response(200)

    client = make_client(handler)
    assert (await client.get("http://a/page")).is_success
    with pytest.raises(PermanentError, match="robots"):
        await client.get("http://a/private/x")


async def test_missing_robots_means_allowed():
    def handler(request):
        return httpx.Response(404 if request.url.path == "/robots.txt" else 200)

    assert (await make_client(handler).get("http://a/page")).is_success
