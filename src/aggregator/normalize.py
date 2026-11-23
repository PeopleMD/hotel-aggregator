"""Pure functions: clean names and prices, decide whether two hotels are the same."""

import math
import re
import unicodedata
from decimal import Decimal

from rapidfuzz import fuzz

GENERIC_WORDS = {"hotel", "hotels", "hostel", "boutique", "the", "guesthouse", "motel"}

CURRENCIES = {
    "€": "EUR",
    "eur": "EUR",
    "euro": "EUR",
    "$": "USD",
    "usd": "USD",
    "lei": "MDL",
    "mdl": "MDL",
    "ron": "RON",
    "uah": "UAH",
    "₴": "UAH",
}
_CUR = r"€|\$|₴|\b(?:eur|euro|usd|lei|mdl|ron|uah)\b"
_NUM = r"\d{1,3}(?:[ ,]\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?"
_PRICE = re.compile(
    rf"(?P<c1>{_CUR})\s*(?P<n1>{_NUM})"
    rf"|(?P<n2>{_NUM})(?:\s*[-–]\s*(?:{_NUM}))?\s*(?P<c2>{_CUR})",
    re.IGNORECASE,
)

_NAME_STARS = re.compile(r"\b([1-5])\s?\*")

NEAR_METERS = 400
NEAR_SCORE = 82
# Without coordinates only an almost identical name is convincing.
NAME_ONLY_SCORE = 95


def normalize_name(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", _NAME_STARS.sub(" ", name).casefold())
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    words = re.findall(r"\w+", plain)
    significant = [word for word in words if word not in GENERIC_WORDS]
    return " ".join(significant or words)


def parse_price(text: str, *, lei: str = "MDL") -> tuple[Decimal, str] | None:
    """First price in free text: 'From €8 for dorm' -> (8, EUR), '30-40 lei' -> (30, MDL).

    A number without a currency is not a price we can trust, so it is ignored.
    """
    match = _PRICE.search(text)
    if match is None:
        return None
    number = match["n1"] or match["n2"]
    symbol = (match["c1"] or match["c2"]).lower()
    amount = Decimal(re.sub(r"[ ,]", "", number))
    currency = lei if symbol == "lei" else CURRENCIES[symbol]
    return (amount, currency) if amount > 0 else None


def parse_stars(text: str | None) -> int | None:
    match = re.match(r"\s*([1-5])", text or "")
    return int(match[1]) if match else None


def stars_from_name(name: str) -> int | None:
    match = _NAME_STARS.search(name)
    return int(match[1]) if match else None


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in meters."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(a))


def match_score(
    name1: str,
    point1: tuple[float, float] | None,
    name2: str,
    point2: tuple[float, float] | None,
) -> float:
    """0 = different hotels, otherwise the name similarity. Names must be normalized."""
    score = fuzz.token_sort_ratio(name1, name2)
    if point1 and point2:
        is_same = score >= NEAR_SCORE and distance_m(*point1, *point2) <= NEAR_METERS
    else:
        is_same = score >= NAME_ONLY_SCORE
    return score if is_same else 0.0
