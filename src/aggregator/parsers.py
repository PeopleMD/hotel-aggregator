"""Each source = `fetch_*` (network) + `parse_*` (pure function, easy to test on saved pages)."""

import json
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from aggregator.cities import City
from aggregator.config import get_settings
from aggregator.http import PermanentError, PoliteClient, TemporaryError
from aggregator.normalize import parse_price, parse_stars, stars_from_name

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
MAX_PAGES = 50


class RawOffer(BaseModel):
    external_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=300)
    url: str
    lat: float | None = None
    lon: float | None = None
    stars: int | None = None
    price: Decimal | None = None
    currency: str | None = None

    @property
    def point(self) -> tuple[float, float] | None:
        if self.lat is None or self.lon is None:
            return None
        return self.lat, self.lon


# --- Wikivoyage: HTML, CC BY-SA ---------------------------------------------------------


def parse_wikivoyage(html: str, city: City, page_url: str) -> list[RawOffer]:
    soup = BeautifulSoup(html, "lxml")
    section = soup.select_one('section[aria-labelledby="Sleep"]')
    if section is None:
        raise PermanentError(f"no Sleep section on {page_url}, markup changed?")
    offers = []
    for card in section.select(".vcard"):
        title = card.select_one(".listing-name")
        if title is None or not title.get_text(strip=True):
            continue
        name = title.get_text(" ", strip=True)
        anchor = str(title.get("id") or name.replace(" ", "_"))
        marker = card.select_one("[data-lat][data-lon]")
        price_tag = card.select_one(".listing-price")
        lei = "RON" if city.slug == "iasi" else "MDL"
        price = parse_price(price_tag.get_text(" ", strip=True), lei=lei) if price_tag else None
        offers.append(
            RawOffer(
                external_id=f"{city.slug}:{anchor}"[:200],
                name=name,
                url=f"{page_url}#{anchor}",
                lat=float(str(marker["data-lat"])) if marker else None,
                lon=float(str(marker["data-lon"])) if marker else None,
                stars=stars_from_name(name),
                price=price[0] if price else None,
                currency=price[1] if price else None,
            )
        )
    return offers


async def fetch_wikivoyage(city: City, client: PoliteClient) -> list[RawOffer]:
    if not get_settings().contact_url:
        raise PermanentError("set CONTACT_URL: Wikimedia requires a contact in the User-Agent")
    url = f"https://en.wikivoyage.org/wiki/{quote(city.wikivoyage_title)}"
    response = await client.get(url)
    return parse_wikivoyage(response.text, city, url)


# --- OpenStreetMap through Overpass: JSON API, ODbL --------------------------------------


def parse_osm(payload: dict[str, Any]) -> list[RawOffer]:
    offers = []
    for element in payload["elements"]:
        tags = element.get("tags", {})
        name = tags.get("name:en") or tags.get("name")
        if not name:
            continue
        point = element if "lat" in element else element.get("center", {})
        ref = f"{element['type']}/{element['id']}"
        offers.append(
            RawOffer(
                external_id=ref,
                name=name,
                url=f"https://www.openstreetmap.org/{ref}",
                lat=point.get("lat"),
                lon=point.get("lon"),
                stars=parse_stars(tags.get("stars")),
            )
        )
    return offers


async def fetch_osm(city: City, client: PoliteClient) -> list[RawOffer]:
    bbox = ",".join(map(str, city.bbox))
    query = f'[out:json][timeout:30];nwr["tourism"~"^(hotel|hostel)$"]({bbox});out center;'
    response = await client.post(OVERPASS_URL, data={"data": query})
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        # An overloaded Overpass can answer 200 with an HTML error page.
        raise TemporaryError("Overpass returned invalid JSON") from exc
    if "remark" in payload and not payload.get("elements"):
        raise TemporaryError(f"Overpass: {payload['remark']}")
    return parse_osm(payload)


# --- Demo supplier: our own fake site with invented hotels, see demo_supplier/ -----------


def parse_demo(html: str, page_url: str) -> tuple[list[RawOffer], str | None]:
    soup = BeautifulSoup(html, "lxml")
    offers = []
    for card in soup.select("article.hotel"):
        link = card.select_one("a.name")
        price_tag = card.select_one(".price")
        if link is None or price_tag is None:
            raise PermanentError(f"unexpected card on {page_url}, markup changed?")
        price = parse_price(price_tag.get_text(strip=True))
        offers.append(
            RawOffer(
                external_id=str(card["data-id"]),
                name=link.get_text(strip=True),
                url=urljoin(page_url, str(link["href"])),
                lat=float(str(card["data-lat"])),
                lon=float(str(card["data-lon"])),
                stars=parse_stars(str(card["data-stars"])),
                price=price[0] if price else None,
                currency=price[1] if price else None,
            )
        )
    next_link = soup.select_one("a[rel=next]")
    return offers, urljoin(page_url, str(next_link["href"])) if next_link else None


async def fetch_demo(city: City, client: PoliteClient) -> list[RawOffer]:
    url: str | None = f"{get_settings().demo_supplier_url}/{city.slug}/hotels"
    offers: list[RawOffer] = []
    for _ in range(MAX_PAGES):
        if url is None:
            return offers
        response = await client.get(url, check_robots=False)
        page, url = parse_demo(response.text, url)
        offers.extend(page)
    raise PermanentError("demo supplier: too many pages, pagination loop?")


Fetcher = Callable[[City, PoliteClient], Awaitable[list[RawOffer]]]

SOURCES: dict[str, Fetcher] = {
    "wikivoyage": fetch_wikivoyage,
    "osm": fetch_osm,
    "demo": fetch_demo,
}


def enabled_sources() -> list[str]:
    has_contact = bool(get_settings().contact_url)
    return [source for source in SOURCES if source != "wikivoyage" or has_contact]
