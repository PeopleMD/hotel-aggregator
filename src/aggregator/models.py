from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

ACTIVE_STATUSES = ("queued", "running", "retrying")


class Base(DeclarativeBase):
    pass


def created_column() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    is_admin: Mapped[bool] = mapped_column(server_default=text("false"))
    created_at: Mapped[datetime] = created_column()


class Hotel(Base):
    """One real-world hotel, merged from the offers of all sources."""

    __tablename__ = "hotels"
    __table_args__ = (
        Index("ix_hotels_city_name", "city", "normalized_name", "id"),
        Index(
            "ix_hotels_name_trgm",
            "normalized_name",
            postgresql_using="gin",
            postgresql_ops={"normalized_name": "gin_trgm_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    normalized_name: Mapped[str] = mapped_column(String(300))
    city: Mapped[str] = mapped_column(String(80))
    lat: Mapped[float | None]
    lon: Mapped[float | None]
    stars: Mapped[int | None]
    created_at: Mapped[datetime] = created_column()

    offers: Mapped[list["Offer"]] = relationship(back_populates="hotel")


class Offer(Base):
    """A hotel as one particular source describes it."""

    __tablename__ = "offers"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_offers_source_external"),
        CheckConstraint("price IS NULL OR price > 0", name="ck_offers_price_positive"),
        CheckConstraint("(price IS NULL) = (currency IS NULL)", name="ck_offers_price_currency"),
        Index("ix_offers_hotel", "hotel_id"),
        Index(
            "ix_offers_currency_price",
            "currency",
            "price",
            postgresql_where=text("is_active AND price IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    hotel_id: Mapped[int] = mapped_column(ForeignKey("hotels.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(30))
    external_id: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(Text)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    is_active: Mapped[bool] = mapped_column(server_default=text("true"))
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    hotel: Mapped[Hotel] = relationship(back_populates="offers")


class PricePoint(Base):
    """Written only when the price changes, so the table stays small."""

    __tablename__ = "price_history"
    __table_args__ = (Index("ix_price_history_offer_time", "offer_id", "observed_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id", ondelete="CASCADE"))
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))
    observed_at: Mapped[datetime] = created_column()


class ParseJob(Base):
    __tablename__ = "parse_jobs"
    __table_args__ = (
        # At most one unfinished job per city and source.
        Index(
            "uq_parse_jobs_active",
            "city",
            "source",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running', 'retrying')"),
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'retrying', 'done', 'failed')",
            name="ck_parse_jobs_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    city: Mapped[str] = mapped_column(String(80))
    source: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(10), server_default="queued")
    attempts: Mapped[int] = mapped_column(server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
