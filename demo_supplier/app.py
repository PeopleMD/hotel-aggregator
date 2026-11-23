"""A fake hotel site to practise on. All hotels and prices here are invented.

Real open sources rarely change prices, so this site exists to show price history,
pagination and behaviour on failures (a share of requests gets HTTP 503).
"""

import os
import random
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

FAIL_RATE = float(os.environ.get("FAIL_RATE", "0.1"))
PRICE_PERIOD = int(os.environ.get("PRICE_PERIOD_SECONDS", "3600"))
TOTAL, PER_PAGE = 60, 20
CENTERS = {"chisinau": (47.02, 28.83), "iasi": (47.16, 27.59), "lviv": (49.84, 24.03)}
FIRST = ["Amber", "Silver", "Linden", "Orchard", "Harbor", "Maple", "Falcon", "Velvet"]
SECOND = ["Court", "Garden", "House", "Plaza", "Terrace", "Lodge", "Rooms", "Residence"]

app = FastAPI(title="Demo supplier")


def hotel(city: str, number: int) -> dict[str, object]:
    stable = random.Random(f"{city}:{number}")
    lat, lon = CENTERS[city]
    period = int(time.time()) // PRICE_PERIOD
    factor = random.Random(f"{city}:{number}:{period}").uniform(0.85, 1.15)
    name = f"Demo {FIRST[number % 8]} {SECOND[number // 8 % 8]}"
    return {
        "id": f"{city}-{number}",
        "name": name,
        "stars": stable.randint(2, 5),
        "lat": round(lat + stable.uniform(-0.03, 0.03), 5),
        "lon": round(lon + stable.uniform(-0.04, 0.04), 5),
        "price": round(stable.uniform(25, 160) * factor),
    }


@app.get("/{city}/hotels", response_class=HTMLResponse)
def hotels(city: str, page: int = 1) -> str:
    if city not in CENTERS or not 1 <= page <= TOTAL // PER_PAGE:
        raise HTTPException(404)
    if random.random() < FAIL_RATE:
        raise HTTPException(503, headers={"Retry-After": "1"})
    start = (page - 1) * PER_PAGE
    cards = "".join(
        f'<article class="hotel" data-id="{h["id"]}" data-lat="{h["lat"]}" '
        f'data-lon="{h["lon"]}" data-stars="{h["stars"]}">'
        f'<a class="name" href="/hotel/{h["id"]}">{h["name"]}</a>'
        f'<span class="price">€{h["price"]}</span></article>'
        for h in (hotel(city, n) for n in range(start, start + PER_PAGE))
    )
    more = f'<a rel="next" href="?page={page + 1}">next</a>' if start + PER_PAGE < TOTAL else ""
    return f"<!doctype html><title>Demo hotels</title><main>{cards}</main>{more}"
