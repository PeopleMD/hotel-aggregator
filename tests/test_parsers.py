from decimal import Decimal

import pytest

from aggregator.cities import CITIES
from aggregator.http import PermanentError
from aggregator.parsers import (
    enabled_sources,
    fetch_wikivoyage,
    parse_demo,
    parse_osm,
    parse_wikivoyage,
)

CITY = CITIES["chisinau"]


def test_wikivoyage_real_page(data):
    html = (data / "wikivoyage_chisinau.html").read_text(encoding="utf8")
    offers = {o.name: o for o in parse_wikivoyage(html, CITY, "https://example.org/page")}
    assert len(offers) == 19
    hotel = offers["Hotel Chișinău"]
    assert (hotel.price, hotel.currency) == (Decimal("28"), "EUR")
    assert hotel.point == (47.0133, 28.8493)
    assert hotel.url == "https://example.org/page#Hotel_Chișinău"
    assert offers["Retro Moldova Hostel"].point is None
    assert offers["Flowers Hotel 4*"].stars == 4


def test_wikivoyage_changed_markup():
    with pytest.raises(PermanentError, match="markup"):
        parse_wikivoyage("<html><body>new design</body></html>", CITY, "u")


async def test_wikivoyage_needs_contact():
    assert enabled_sources() == ["osm", "demo"]
    with pytest.raises(PermanentError, match="CONTACT_URL"):
        await fetch_wikivoyage(CITY, None)


def test_osm():
    payload = {
        "elements": [
            {
                "type": "node",
                "id": 1,
                "lat": 47.0,
                "lon": 28.8,
                "tags": {"name": "Отель", "name:en": "VisPas Hotel", "stars": "4S"},
            },
            {"type": "way", "id": 2, "center": {"lat": 47.1, "lon": 28.9}, "tags": {"name": "B"}},
            {"type": "node", "id": 3, "lat": 1, "lon": 1, "tags": {"tourism": "hotel"}},
        ]
    }
    first, second = parse_osm(payload)  # the nameless one is skipped
    assert (first.external_id, first.name, first.stars) == ("node/1", "VisPas Hotel", 4)
    assert second.point == (47.1, 28.9)


CARD = (
    '<article class="hotel" data-id="c-1" data-lat="47.0" data-lon="28.8" data-stars="3">'
    '<a class="name" href="/hotel/c-1">Demo Inn</a><span class="price">€55</span></article>'
)


def test_demo_page_and_pagination():
    url = "http://demo/chisinau/hotels"
    offers, next_url = parse_demo(CARD + '<a rel="next" href="?page=2">next</a>', url)
    assert offers[0].url == "http://demo/hotel/c-1"
    assert (offers[0].price, offers[0].currency) == (Decimal("55"), "EUR")
    assert next_url == "http://demo/chisinau/hotels?page=2"
    assert parse_demo(CARD, url)[1] is None


def test_demo_changed_markup():
    with pytest.raises(PermanentError):
        parse_demo(CARD.replace('class="price"', 'class="cost"'), "http://demo/")
