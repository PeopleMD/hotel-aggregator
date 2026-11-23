"""Saves parsed offers: merges duplicates, updates prices, keeps price history."""

from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from aggregator.models import Hotel, Offer, PricePoint
from aggregator.normalize import match_score, normalize_name
from aggregator.parsers import RawOffer

BATCH = 1000


@dataclass
class SaveStats:
    offers: int = 0
    new_hotels: int = 0
    merged: int = 0
    price_changes: int = 0
    deactivated: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


async def save_offers(
    session: AsyncSession, city: str, source: str, raw_offers: list[RawOffer]
) -> SaveStats:
    """Does not commit: the caller commits offers and job status together."""
    # Two workers saving the same city at once would create duplicate hotels,
    # so they take turns. The lock is released at the end of the transaction.
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(city))))

    # A source may list the same id twice; ON CONFLICT cannot touch a row twice.
    unique = list({raw.external_id: raw for raw in raw_offers}.values())
    stats = SaveStats(offers=len(unique))

    known = {
        offer.external_id: offer
        for offer in await session.scalars(
            select(Offer)
            .join(Hotel)
            .where(
                Offer.source == source,
                or_(Hotel.city == city, Offer.external_id.in_([r.external_id for r in unique])),
            )
        )
    }
    hotel_by_offer = await _assign_hotels(session, city, source, unique, known, stats)

    rows: list[dict[str, Any]] = [
        {
            "hotel_id": hotel_by_offer[raw.external_id],
            "source": source,
            "external_id": raw.external_id,
            "name": raw.name,
            "url": raw.url,
            "price": raw.price,
            "currency": raw.currency,
        }
        for raw in unique
    ]
    old_prices = {key: (offer.price, offer.currency) for key, offer in known.items()}
    offer_ids = await _upsert(session, rows)

    changes = [
        {"offer_id": offer_ids[raw.external_id], "price": raw.price, "currency": raw.currency}
        for raw in unique
        if raw.price is not None and old_prices.get(raw.external_id) != (raw.price, raw.currency)
    ]
    for start in range(0, len(changes), BATCH):
        await session.execute(insert(PricePoint), changes[start : start + BATCH])
    stats.price_changes = len(changes)

    seen = {raw.external_id for raw in unique}
    gone = [o.id for key, o in known.items() if key not in seen and o.is_active]
    if gone:
        await session.execute(update(Offer).where(Offer.id.in_(gone)).values(is_active=False))
    stats.deactivated = len(gone)
    return stats


async def _assign_hotels(
    session: AsyncSession,
    city: str,
    source: str,
    raw_offers: list[RawOffer],
    known: dict[str, Offer],
    stats: SaveStats,
) -> dict[str, int]:
    """external_id -> hotel id; creates hotels that match nothing."""
    result = {key: offer.hotel_id for key, offer in known.items()}
    hotels = list(await session.scalars(select(Hotel).where(Hotel.city == city)))
    # Two records of one source are two different hotels, never merge them.
    taken = set(result.values())
    created: list[tuple[RawOffer, Hotel]] = []

    for raw in raw_offers:
        if raw.external_id in result:
            continue
        normalized = normalize_name(raw.name)
        best, best_score = None, 0.0
        for hotel in hotels:
            if hotel.id in taken:
                continue
            point = (
                (hotel.lat, hotel.lon) if hotel.lat is not None and hotel.lon is not None else None
            )
            score = match_score(normalized, raw.point, hotel.normalized_name, point)
            if score > best_score:
                best, best_score = hotel, score
        if best is not None:
            result[raw.external_id] = best.id
            taken.add(best.id)
            stats.merged += 1
            # Fill the gaps with what the other source knows.
            if best.lat is None or best.lon is None:
                best.lat, best.lon = raw.lat, raw.lon
            if best.stars is None:
                best.stars = raw.stars
        else:
            hotel = Hotel(
                name=raw.name,
                normalized_name=normalized,
                city=city,
                lat=raw.lat,
                lon=raw.lon,
                stars=raw.stars,
            )
            session.add(hotel)
            created.append((raw, hotel))

    await session.flush()
    for raw, hotel in created:
        result[raw.external_id] = hotel.id
    stats.new_hotels = len(created)
    return result


async def _upsert(session: AsyncSession, rows: list[dict[str, Any]]) -> dict[str, int]:
    ids: dict[str, int] = {}
    for start in range(0, len(rows), BATCH):
        stmt = pg_insert(Offer).values(rows[start : start + BATCH])
        stmt = stmt.on_conflict_do_update(
            constraint="uq_offers_source_external",
            set_={
                "name": stmt.excluded.name,
                "url": stmt.excluded.url,
                "price": stmt.excluded.price,
                "currency": stmt.excluded.currency,
                "is_active": True,
                "last_seen_at": func.now(),
            },
        )
        returned = await session.execute(stmt.returning(Offer.external_id, Offer.id))
        ids.update({external_id: offer_id for external_id, offer_id in returned})
    return ids
