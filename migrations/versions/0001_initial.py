"""initial"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table(
        "hotels",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("normalized_name", sa.String(length=300), nullable=False),
        sa.Column("city", sa.String(length=80), nullable=False),
        sa.Column("lat", sa.Double(), nullable=True),
        sa.Column("lon", sa.Double(), nullable=True),
        sa.Column("stars", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_hotels_city_name", "hotels", ["city", "normalized_name", "id"], unique=False
    )
    op.create_index(
        "ix_hotels_name_trgm",
        "hotels",
        ["normalized_name"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"normalized_name": "gin_trgm_ops"},
    )
    op.create_table(
        "parse_jobs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("city", sa.String(length=80), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=10), server_default="queued", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("stats", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'retrying', 'done', 'failed')",
            name="ck_parse_jobs_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_parse_jobs_active",
        "parse_jobs",
        ["city", "source"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running', 'retrying')"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("password_hash", sa.String(length=200), nullable=False),
        sa.Column("is_admin", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_table(
        "offers",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("hotel_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("external_id", sa.String(length=200), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("(price IS NULL) = (currency IS NULL)", name="ck_offers_price_currency"),
        sa.CheckConstraint("price IS NULL OR price > 0", name="ck_offers_price_positive"),
        sa.ForeignKeyConstraint(["hotel_id"], ["hotels.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "external_id", name="uq_offers_source_external"),
    )
    op.create_index(
        "ix_offers_currency_price",
        "offers",
        ["currency", "price"],
        unique=False,
        postgresql_where=sa.text("is_active AND price IS NOT NULL"),
    )
    op.create_index("ix_offers_hotel", "offers", ["hotel_id"], unique=False)
    op.create_table(
        "price_history",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("offer_id", sa.BigInteger(), nullable=False),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["offer_id"], ["offers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_price_history_offer_time", "price_history", ["offer_id", "observed_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_price_history_offer_time", table_name="price_history")
    op.drop_table("price_history")
    op.drop_index("ix_offers_hotel", table_name="offers")
    op.drop_index(
        "ix_offers_currency_price",
        table_name="offers",
        postgresql_where=sa.text("is_active AND price IS NOT NULL"),
    )
    op.drop_table("offers")
    op.drop_table("users")
    op.drop_index(
        "uq_parse_jobs_active",
        table_name="parse_jobs",
        postgresql_where=sa.text("status IN ('queued', 'running', 'retrying')"),
    )
    op.drop_table("parse_jobs")
    op.drop_index(
        "ix_hotels_name_trgm",
        table_name="hotels",
        postgresql_using="gin",
        postgresql_ops={"normalized_name": "gin_trgm_ops"},
    )
    op.drop_index("ix_hotels_city_name", table_name="hotels")
    op.drop_table("hotels")
