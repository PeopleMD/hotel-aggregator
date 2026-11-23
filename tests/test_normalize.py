from decimal import Decimal

import pytest

from aggregator.normalize import (
    distance_m,
    match_score,
    normalize_name,
    parse_price,
    parse_stars,
    stars_from_name,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Hotel Chișinău", "chisinau"),
        ("VisPas Hotel", "vispas"),
        ("Flowers Hotel 4*", "flowers"),
        ("Leogrand Hotel & Conventions Center", "leogrand conventions center"),
        ("Отель Дачия", "отель дачия"),
        ("The Hotel", "the hotel"),  # nothing left without generic words
    ],
)
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Rooms from about €28", (Decimal("28"), "EUR")),
        ("From €8 for basic dorm to €11.50 p.p. for twin private", (Decimal("8"), "EUR")),
        ("30-40 lei", (Decimal("30"), "MDL")),
        ("1 200 lei", (Decimal("1200"), "MDL")),
        ("1,250.50 USD", (Decimal("1250.50"), "USD")),
        ("€0", None),
        ("150-400", None),  # no currency
        ("Mid-range to splurge", None),
        ("", None),
    ],
)
def test_parse_price(text, expected):
    assert parse_price(text) == expected


def test_lei_depends_on_country():
    assert parse_price("300 lei", lei="RON") == (Decimal("300"), "RON")


@pytest.mark.parametrize(
    ("text", "expected"), [("4", 4), ("4S", 4), ("0", None), ("x", None), (None, None)]
)
def test_parse_stars(text, expected):
    assert parse_stars(text) == expected


def test_stars_from_name():
    assert stars_from_name("Flowers Hotel 4*") == 4
    assert stars_from_name("Route 66") is None


def test_distance():
    assert distance_m(47.0, 28.8, 47.0, 28.8) == 0
    assert distance_m(47.0, 28.8, 47.001, 28.8) == pytest.approx(111, abs=1)


HERE, NEAR, FAR = (47.0322, 28.8191), (47.0321991, 28.8192228), (47.10, 28.90)


@pytest.mark.parametrize(
    ("name1", "point1", "name2", "point2", "same"),
    [
        ("vispas", HERE, "vispas", NEAR, True),
        ("vispas", HERE, "vispass", NEAR, True),
        ("vispas", HERE, "vispas", FAR, False),  # same name, other end of the city
        ("vispas", HERE, "budapest", NEAR, False),  # neighbours
        ("city", HERE, "city park", NEAR, False),
        ("vispas", None, "vispas", HERE, True),
        ("vispas", None, "vispass", HERE, False),  # no coordinates: be strict
    ],
)
def test_match(name1, point1, name2, point2, same):
    assert bool(match_score(name1, point1, name2, point2)) is same
