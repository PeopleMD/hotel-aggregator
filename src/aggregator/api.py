import base64
import binascii
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from aio_pika.abc import AbstractChannel
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from aggregator import queue
from aggregator.auth import admin_user, authenticate, create_token
from aggregator.cities import CITIES
from aggregator.db import get_session
from aggregator.jobs import create_job
from aggregator.models import Hotel, Offer, ParseJob, PricePoint
from aggregator.normalize import normalize_name
from aggregator.parsers import enabled_sources

log = logging.getLogger("api")
Session = Annotated[AsyncSession, Depends(get_session)]
MAX_ID = 2**63 - 1


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with await queue.connect() as connection:
        channel = await connection.channel()
        await queue.declare(channel)
        app.state.channel = channel
        yield


app = FastAPI(title="Hotel aggregator", version="0.1.0", lifespan=lifespan)


def get_channel(request: Request) -> AbstractChannel:
    return request.app.state.channel


# --- schemas ------------------------------------------------------------------------------


class _FromOrm(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class HotelItem(BaseModel):
    id: int
    name: str
    city: str
    stars: int | None
    lat: float | None
    lon: float | None
    offers: int
    min_price: Decimal | None
    currency: str | None


class HotelPage(BaseModel):
    items: list[HotelItem]
    next_cursor: str | None


class OfferOut(_FromOrm):
    id: int
    source: str
    name: str
    url: str
    price: Decimal | None
    currency: str | None
    is_active: bool
    last_seen_at: datetime


class HotelOut(_FromOrm):
    id: int
    name: str
    city: str
    stars: int | None
    lat: float | None
    lon: float | None
    offers: list[OfferOut]


class PriceOut(_FromOrm):
    offer_id: int
    source: str
    price: Decimal
    currency: str
    observed_at: datetime


class JobIn(BaseModel):
    city: str
    source: str


class JobOut(_FromOrm):
    id: int
    city: str
    source: str
    status: str
    attempts: int
    error: str | None
    stats: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105


# --- cursor -------------------------------------------------------------------------------


def encode_cursor(value: object, hotel_id: int) -> str:
    return base64.urlsafe_b64encode(json.dumps([str(value), hotel_id]).encode()).decode()


def decode_cursor(cursor: str) -> tuple[str, int]:
    try:
        value, hotel_id = json.loads(base64.urlsafe_b64decode(cursor))
    except (binascii.Error, ValueError, TypeError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Bad cursor") from None
    valid_id = type(hotel_id) is int and 0 <= hotel_id <= MAX_ID
    if not isinstance(value, str) or "\x00" in value or not valid_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Bad cursor")
    return value, hotel_id


def decode_price_cursor(cursor: str) -> tuple[Decimal, int]:
    value, hotel_id = decode_cursor(cursor)
    try:
        price = Decimal(value)
    except InvalidOperation:
        price = Decimal("NaN")
    if not price.is_finite():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Bad cursor")
    return price, hotel_id


# --- hotels -------------------------------------------------------------------------------


class HotelFilters(BaseModel):
    city: str | None = None
    q: str | None = None
    stars_min: int | None = None
    source: str | None = None
    currency: str | None = None
    price_min: Decimal | None = None
    price_max: Decimal | None = None
    sort: Literal["name", "price"] = "name"


def hotel_filters(
    city: Annotated[str | None, Query(max_length=80)] = None,
    q: Annotated[str | None, Query(min_length=2, max_length=100)] = None,
    stars_min: Annotated[int | None, Query(ge=1, le=5)] = None,
    source: Annotated[str | None, Query(max_length=30)] = None,
    currency: Annotated[str | None, Query(pattern="^[A-Z]{3}$")] = None,
    price_min: Annotated[Decimal | None, Query(ge=0, le=10**9)] = None,
    price_max: Annotated[Decimal | None, Query(ge=0, le=10**9)] = None,
    sort: Literal["name", "price"] = "name",
) -> HotelFilters:
    needs_currency = sort == "price" or price_min is not None or price_max is not None
    if needs_currency and currency is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Prices in different currencies cannot be compared: pass `currency`",
        )
    if any("\x00" in value for value in (city, q, source) if value):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Bad characters")
    return HotelFilters(
        city=city,
        q=q,
        stars_min=stars_min,
        source=source,
        currency=currency,
        price_min=price_min,
        price_max=price_max,
        sort=sort,
    )


@app.get("/hotels", response_model=HotelPage, tags=["hotels"])
async def list_hotels(
    session: Session,
    filters: Annotated[HotelFilters, Depends(hotel_filters)],
    cursor: Annotated[str | None, Query(max_length=1000)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> HotelPage:
    """Keyset pagination: pass `next_cursor` from the previous page."""
    min_price = func.min(Offer.price).filter(Offer.currency == filters.currency)
    agg = (
        select(
            Offer.hotel_id,
            func.count().label("offers"),
            min_price.label("min_price"),
            func.bool_or(Offer.source == filters.source).label("has_source"),
        )
        .where(Offer.is_active)
        .group_by(Offer.hotel_id)
        .subquery()
    )
    stmt = select(Hotel, agg.c.offers, agg.c.min_price).join(agg, agg.c.hotel_id == Hotel.id)
    if filters.city:
        stmt = stmt.where(Hotel.city == filters.city)
    if filters.q:
        # Trigram word similarity, served by the GIN index; survives typos.
        stmt = stmt.where(Hotel.normalized_name.op("%>")(normalize_name(filters.q)))
    if filters.stars_min:
        stmt = stmt.where(Hotel.stars >= filters.stars_min)
    if filters.source:
        stmt = stmt.where(agg.c.has_source)
    if filters.price_min is not None:
        stmt = stmt.where(agg.c.min_price >= filters.price_min)
    if filters.price_max is not None:
        stmt = stmt.where(agg.c.min_price <= filters.price_max)

    if filters.sort == "price":
        stmt = stmt.where(agg.c.min_price.is_not(None)).order_by(agg.c.min_price, Hotel.id)
        if cursor:
            stmt = stmt.where(tuple_(agg.c.min_price, Hotel.id) > decode_price_cursor(cursor))
    else:
        stmt = stmt.order_by(Hotel.normalized_name, Hotel.id)
        if cursor:
            stmt = stmt.where(tuple_(Hotel.normalized_name, Hotel.id) > decode_cursor(cursor))

    # One extra row tells whether there is a next page without COUNT(*).
    rows = (await session.execute(stmt.limit(limit + 1))).all()
    page = rows[:limit]
    items = [
        HotelItem(
            id=hotel.id,
            name=hotel.name,
            city=hotel.city,
            stars=hotel.stars,
            lat=hotel.lat,
            lon=hotel.lon,
            offers=offers,
            min_price=price,
            currency=filters.currency if price is not None else None,
        )
        for hotel, offers, price in page
    ]
    next_cursor = None
    if len(rows) > limit:
        last, _, last_price = page[-1]
        value = last_price if filters.sort == "price" else last.normalized_name
        next_cursor = encode_cursor(value, last.id)
    return HotelPage(items=items, next_cursor=next_cursor)


@app.get("/hotels/{hotel_id}", response_model=HotelOut, tags=["hotels"])
async def get_hotel(hotel_id: int, session: Session) -> Hotel:
    hotel = None
    if 0 < hotel_id <= MAX_ID:
        hotel = await session.get(Hotel, hotel_id, options=[selectinload(Hotel.offers)])
    if hotel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Hotel not found")
    return hotel


@app.get("/hotels/{hotel_id}/prices", response_model=list[PriceOut], tags=["hotels"])
async def get_prices(
    hotel_id: int, session: Session, limit: Annotated[int, Query(ge=1, le=1000)] = 200
) -> list[PriceOut]:
    if not 0 < hotel_id <= MAX_ID or await session.get(Hotel, hotel_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Hotel not found")
    rows = await session.execute(
        select(
            PricePoint.offer_id,
            Offer.source,
            PricePoint.price,
            PricePoint.currency,
            PricePoint.observed_at,
        )
        .join(Offer, Offer.id == PricePoint.offer_id)
        .where(Offer.hotel_id == hotel_id)
        .order_by(PricePoint.observed_at.desc(), PricePoint.id.desc())
        .limit(limit)
    )
    return [PriceOut.model_validate(row) for row in rows]


# --- auth and jobs ------------------------------------------------------------------------


@app.post("/auth/token", response_model=Token, tags=["auth"])
async def login(form: Annotated[OAuth2PasswordRequestForm, Depends()], session: Session) -> Token:
    user = None
    if "\x00" not in form.username:
        user = await authenticate(session, form.username, form.password)
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Wrong email or password",
            {"WWW-Authenticate": "Bearer"},
        )
    return Token(access_token=create_token(user.id))


@app.post(
    "/jobs",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["jobs"],
    dependencies=[Depends(admin_user)],
)
async def start_job(
    body: JobIn, session: Session, channel: Annotated[AbstractChannel, Depends(get_channel)]
) -> ParseJob:
    if body.city not in CITIES or body.source not in enabled_sources():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"city: {sorted(CITIES)}, source: {enabled_sources()}",
        )
    job = await create_job(session, channel, body.city, body.source)
    if job is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Such a job is already in progress")
    return job


@app.get("/jobs", response_model=list[JobOut], tags=["jobs"], dependencies=[Depends(admin_user)])
async def list_jobs(
    session: Session, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[ParseJob]:
    return list(await session.scalars(select(ParseJob).order_by(ParseJob.id.desc()).limit(limit)))


@app.get("/jobs/{job_id}", response_model=JobOut, tags=["jobs"], dependencies=[Depends(admin_user)])
async def get_job(job_id: int, session: Session) -> ParseJob:
    job = await session.get(ParseJob, job_id) if 0 < job_id <= MAX_ID else None
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return job


# --- health -------------------------------------------------------------------------------


@app.get("/health", tags=["service"])
async def health(session: Session, request: Request) -> dict[str, str]:
    await session.execute(text("SELECT 1"))
    if request.app.state.channel.is_closed:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "RabbitMQ is down")
    return {"status": "ok"}
