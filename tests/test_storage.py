import asyncio
from decimal import Decimal

from sqlalchemy import func, select

from aggregator.db import get_sessionmaker
from aggregator.models import Hotel, Offer, PricePoint
from aggregator.parsers import RawOffer
from aggregator.storage import save_offers


def raw(external_id, name="VisPas Hotel", lat=47.0322, lon=28.8191, price=None, **kwargs):
    return RawOffer(
        external_id=external_id,
        name=name,
        url="http://x/",
        lat=lat,
        lon=lon,
        price=price,
        currency="EUR" if price else None,
        **kwargs,
    )


async def count(session, model):
    return await session.scalar(select(func.count()).select_from(model))


async def save(session, source, *offers):
    stats = await save_offers(session, "chisinau", source, list(offers))
    await session.commit()
    return stats


async def test_two_sources_become_one_hotel(session):
    await save(session, "osm", raw("node/1", stars=4))
    stats = await save(session, "wikivoyage", raw("w1", "Hotel VisPas", lat=47.0321, price=50))
    assert (stats.new_hotels, stats.merged) == (0, 1)
    assert (await count(session, Hotel), await count(session, Offer)) == (1, 2)


async def test_second_source_fills_gaps(session):
    await save(session, "wikivoyage", raw("w1", lat=None, lon=None))
    await save(session, "osm", raw("node/1", stars=4))
    hotel = await session.scalar(select(Hotel))
    assert (hotel.lat, hotel.lon, hotel.stars) == (47.0322, 28.8191, 4)


async def test_one_source_is_never_merged_with_itself(session):
    stats = await save(session, "osm", raw("node/1"), raw("node/2"))
    assert stats.new_hotels == 2


async def test_repeat_changes_nothing(session):
    await save(session, "demo", raw("d1", price=50))
    stats = await save(session, "demo", raw("d1", price=50))
    assert (stats.new_hotels, stats.merged, stats.price_changes) == (0, 0, 0)
    assert await count(session, PricePoint) == 1


async def test_price_history_keeps_only_changes(session):
    for price in (50, 50, 60, 50):
        await save(session, "demo", raw("d1", price=price))
    prices = await session.scalars(select(PricePoint.price).order_by(PricePoint.id))
    assert list(prices) == [Decimal(50), Decimal(60), Decimal(50)]


async def test_missing_offer_is_deactivated_and_comes_back(session):
    other = raw("d2", "Budapest", lat=46.95)
    await save(session, "demo", raw("d1"), other)
    stats = await save(session, "demo", other)
    assert stats.deactivated == 1
    by_id = select(Offer.is_active).where(Offer.external_id == "d1")
    assert await session.scalar(by_id) is False
    await save(session, "demo", raw("d1"), other)
    assert await session.scalar(by_id) is True


async def test_other_source_is_not_deactivated(session):
    await save(session, "osm", raw("node/1"))
    stats = await save(session, "demo", raw("d1", "Budapest", lat=46.95))
    assert stats.deactivated == 0


async def test_same_id_twice_in_one_batch(session):
    stats = await save(session, "demo", raw("d1", price=50), raw("d1", price=60))
    assert stats.offers == 1


async def test_second_worker_waits_for_the_first(session):
    async def second_worker():
        async with get_sessionmaker()() as own, own.begin():
            return await save_offers(own, "chisinau", "osm", [raw("node/1")])

    # The first worker has saved the hotel but has not committed yet.
    await save_offers(session, "chisinau", "demo", [raw("d1")])
    second = asyncio.create_task(second_worker())
    await asyncio.sleep(0.3)
    assert not second.done()
    await session.commit()
    assert (await second).merged == 1
    assert (await count(session, Hotel), await count(session, Offer)) == (1, 2)


async def test_big_batch(session):
    offers = [raw(f"d{n}", f"H{n}", lat=n / 100, price=10 + n) for n in range(2500)]
    stats = await save(session, "demo", *offers)
    assert (stats.new_hotels, stats.price_changes) == (2500, 2500)
