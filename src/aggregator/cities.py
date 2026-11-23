from dataclasses import dataclass


@dataclass(frozen=True)
class City:
    slug: str
    wikivoyage_title: str
    # south, west, north, east
    bbox: tuple[float, float, float, float]


CITIES = {
    city.slug: city
    for city in (
        City("chisinau", "Chișinău", (46.93, 28.72, 47.10, 28.97)),
        City("iasi", "Iași", (47.10, 27.50, 47.22, 27.70)),
        City("lviv", "Lviv", (49.77, 23.90, 49.90, 24.12)),
    )
}
