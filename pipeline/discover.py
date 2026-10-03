"""Step 1: find City of Vancouver restaurants with the Google Places API (New)."""

import argparse
import hashlib
import json
import time

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from . import config

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
MAX_PAGES = 3  # Text Search caps at 60 results (3 pages of 20)


def grid_cells(rows: int, cols: int) -> list[dict]:
    south, west, north, east = config.BBOX
    dlat = (north - south) / rows
    dlng = (east - west) / cols
    cells = []
    for r in range(rows):
        for c in range(cols):
            cells.append(
                {
                    "low": {"latitude": south + r * dlat, "longitude": west + c * dlng},
                    "high": {"latitude": south + (r + 1) * dlat, "longitude": west + (c + 1) * dlng},
                }
            )
    return cells


@retry(stop=stop_after_attempt(4), wait=wait_exponential(min=2, max=30))
def _post(client: httpx.Client, body: dict) -> dict:
    resp = client.post(SEARCH_URL, json=body)
    if resp.status_code == 400:
        raise SystemExit(f"Places API rejected the request: {resp.text}")
    resp.raise_for_status()
    return resp.json()


def search(client: httpx.Client, query: str, cell: dict, page_token: str | None) -> dict:
    """Run one Text Search page, caching the raw response on disk."""
    body = {"textQuery": query, "locationRestriction": {"rectangle": cell}, "pageSize": 20}
    if page_token:
        body["pageToken"] = page_token
    key = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:24]
    cache_file = config.CACHE_DIR / "places" / f"{key}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())
    data = _post(client, body)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(data))
    return data


def locality(place: dict) -> str | None:
    for comp in place.get("addressComponents", []):
        if "locality" in comp.get("types", []):
            return comp.get("longText")
    return None


def to_record(place: dict) -> dict:
    return {
        "place_id": place["id"],
        "name": place.get("displayName", {}).get("text"),
        "lat": place["location"]["latitude"],
        "lng": place["location"]["longitude"],
        "address": place.get("formattedAddress"),
        "website": place.get("websiteUri"),
        "google_maps_url": place.get("googleMapsUri"),
        "price_level": place.get("priceLevel"),
        "rating": place.get("rating"),
        "rating_count": place.get("userRatingCount"),
        "primary_type": place.get("primaryType"),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit-cells", type=int, help="only search the first N grid cells (for testing)")
    args = parser.parse_args(argv)

    headers = {
        "X-Goog-Api-Key": config.require_env("GOOGLE_MAPS_API_KEY"),
        "X-Goog-FieldMask": config.PLACES_FIELDS,
    }
    cells = grid_cells(config.GRID_ROWS, config.GRID_COLS)
    if args.limit_cells:
        # Start from the middle of the grid so a small test lands downtown-ish.
        mid = len(cells) // 2
        cells = cells[mid : mid + args.limit_cells]

    places: dict[str, dict] = {r["place_id"]: r for r in config.read_jsonl(config.RESTAURANTS_FILE)}
    before = len(places)
    dropped_locality = 0
    with httpx.Client(headers=headers, timeout=30) as client:
        for i, cell in enumerate(cells, 1):
            for query in config.SEARCH_QUERIES:
                token = None
                for _ in range(MAX_PAGES):
                    data = search(client, query, cell, token)
                    for place in data.get("places", []):
                        if place.get("businessStatus", "OPERATIONAL") != "OPERATIONAL":
                            continue
                        if locality(place) not in config.ALLOWED_LOCALITIES:
                            dropped_locality += 1
                            continue
                        places.setdefault(place["id"], to_record(place))
                    token = data.get("nextPageToken")
                    if not token:
                        break
                    time.sleep(1)  # next-page tokens take a moment to become valid
            print(f"cell {i}/{len(cells)}: {len(places)} restaurants so far")

    config.write_jsonl(config.RESTAURANTS_FILE, sorted(places.values(), key=lambda r: r["place_id"]))
    print(
        f"Saved {len(places)} restaurants ({len(places) - before} new, "
        f"{dropped_locality} results outside the city skipped) -> {config.RESTAURANTS_FILE}"
    )


if __name__ == "__main__":
    main()
