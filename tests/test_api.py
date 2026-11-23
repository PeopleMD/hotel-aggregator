import base64
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest

from aggregator import auth
from aggregator.api import app, get_channel
from aggregator.config import get_settings
from aggregator.models import User
from aggregator.parsers import RawOffer
from aggregator.storage import save_offers

PASSWORD = "correct horse"


@pytest.fixture
async def api(session, channel):
    app.dependency_overrides[get_channel] = lambda: channel
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def add_user(session, email, is_admin):
    user = User(email=email, password_hash=auth.hash_password(PASSWORD), is_admin=is_admin)
    session.add(user)
    await session.commit()
    return {"Authorization": f"Bearer {auth.create_token(user.id)}"}


@pytest.fixture
async def admin(session):
    return await add_user(session, "admin@example.com", True)


@pytest.fixture
async def hotels(session):
    def raw(n, name, price, stars, lat):
        return RawOffer(
            external_id=str(n),
            name=name,
            url="http://x/",
            lat=lat,
            lon=28.8,
            stars=stars,
            price=price,
            currency="EUR" if price else None,
        )

    await save_offers(
        session,
        "chisinau",
        "demo",
        [
            raw(1, "Amber Court", 40, 3, 47.01),
            raw(2, "Budapest", 90, 5, 47.02),
            raw(3, "Cosmos", 40, 4, 47.03),
            raw(4, "Dacia", None, 2, 47.04),
        ],
    )
    await save_offers(session, "chisinau", "osm", [raw(5, "Hotel Cosmos", None, None, 47.03)])
    await save_offers(session, "lviv", "demo", [raw(6, "Edem", 10, 4, 49.8)])
    await session.commit()


async def names(api, **params):
    response = await api.get("/hotels", params=params)
    assert response.status_code == 200, response.text
    return [item["name"] for item in response.json()["items"]]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, ["Amber Court", "Budapest", "Cosmos", "Dacia", "Edem"]),
        ({"city": "lviv"}, ["Edem"]),
        ({"stars_min": 4}, ["Budapest", "Cosmos", "Edem"]),
        ({"source": "osm"}, ["Cosmos"]),
        ({"q": "budapset"}, []),
        ({"q": "budapes"}, ["Budapest"]),
        ({"q": "Hotel Cosmos"}, ["Cosmos"]),
        ({"currency": "EUR", "price_max": 50}, ["Amber Court", "Cosmos", "Edem"]),
        ({"currency": "EUR", "price_min": 41}, ["Budapest"]),
        (
            {"currency": "EUR", "sort": "price"},
            ["Edem", "Amber Court", "Cosmos", "Budapest"],
        ),
        ({"currency": "USD", "sort": "price"}, []),
    ],
)
async def test_filters(api, hotels, params, expected):
    assert await names(api, **params) == expected


@pytest.mark.parametrize("params", [{}, {"sort": "price", "currency": "EUR"}])
async def test_pagination_has_no_gaps_or_repeats(api, hotels, params):
    seen, cursor = [], None
    for _ in range(10):
        query = {**params, "limit": 1, **({"cursor": cursor} if cursor else {})}
        page = (await api.get("/hotels", params=query)).json()
        seen += [item["name"] for item in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == await names(api, **params)


def make_cursor(text):
    return base64.urlsafe_b64encode(text.encode()).decode()


@pytest.mark.parametrize(
    "params",
    [
        {"sort": "price"},
        {"price_max": 10},
        {"stars_min": 9},
        {"limit": 1000},
        {"city": "a\x00b"},
        {"cursor": "%%%"},
        {"cursor": "абв"},
        {"cursor": make_cursor("[1]")},
        {"cursor": make_cursor('["a", "b"]')},
        {"cursor": make_cursor('["a\\u0000", 1]')},
        {"cursor": make_cursor(f'["a", {2**70}]')},
        {"cursor": make_cursor('["abc", 1]'), "sort": "price", "currency": "EUR"},
        {"cursor": make_cursor('["NaN", 1]'), "sort": "price", "currency": "EUR"},
    ],
)
async def test_bad_parameters(api, hotels, params):
    assert (await api.get("/hotels", params=params)).status_code == 422


async def test_hotel_with_offers_and_prices(api, hotels):
    item = (await api.get("/hotels", params={"q": "cosmos"})).json()["items"][0]
    hotel = (await api.get(f"/hotels/{item['id']}")).json()
    assert {offer["source"] for offer in hotel["offers"]} == {"demo", "osm"}
    prices = (await api.get(f"/hotels/{item['id']}/prices")).json()
    assert [(p["price"], p["currency"]) for p in prices] == [("40.00", "EUR")]


@pytest.mark.parametrize("hotel_id", [0, 999, 2**70])
async def test_unknown_hotel(api, hotel_id):
    assert (await api.get(f"/hotels/{hotel_id}")).status_code == 404
    assert (await api.get(f"/hotels/{hotel_id}/prices")).status_code == 404


async def test_login(api, admin):
    async def login(email, password):
        return await api.post("/auth/token", data={"username": email, "password": password})

    response = await login("Admin@Example.com", PASSWORD)
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    assert (await api.get("/jobs", headers=headers)).status_code == 200
    assert (await login("admin@example.com", "wrong")).status_code == 401
    assert (await login("nobody@example.com", PASSWORD)).status_code == 401


async def test_only_admin_starts_jobs(api, session):
    body = {"city": "chisinau", "source": "demo"}
    assert (await api.post("/jobs", json=body)).status_code == 401
    user = await add_user(session, "user@example.com", False)
    assert (await api.post("/jobs", json=body, headers=user)).status_code == 403


@pytest.mark.parametrize("change", [{"exp": -5}, {"key": "another-key-" + "y" * 32}, {"sub": "x"}])
async def test_bad_tokens(api, admin, change):
    payload = {
        "sub": change.get("sub", "1"),
        "exp": datetime.now(UTC) + timedelta(minutes=change.get("exp", 5)),
    }
    token = jwt.encode(payload, change.get("key", get_settings().jwt_secret), "HS256")
    response = await api.get("/jobs", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


async def test_job_lifecycle(api, admin):
    body = {"city": "chisinau", "source": "demo"}
    response = await api.post("/jobs", json=body, headers=admin)
    assert response.status_code == 202
    job = response.json()
    assert job["status"] == "queued"
    assert (await api.post("/jobs", json=body, headers=admin)).status_code == 409
    assert (await api.get(f"/jobs/{job['id']}", headers=admin)).json()["id"] == job["id"]
    assert (await api.get("/jobs/999", headers=admin)).status_code == 404


@pytest.mark.parametrize("body", [{"city": "mars", "source": "demo"}, {"city": "lviv"}])
async def test_bad_job(api, admin, body):
    assert (await api.post("/jobs", json=body, headers=admin)).status_code == 422


async def test_disabled_source(api, admin):
    body = {"city": "lviv", "source": "wikivoyage"}
    assert (await api.post("/jobs", json=body, headers=admin)).status_code == 422
