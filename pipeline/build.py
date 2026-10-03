"""Step 4: merge restaurants, extracted prices and manual overrides into the map's GeoJSON."""

import csv
import json

from . import config

PRICE_LEVELS = {
    "PRICE_LEVEL_INEXPENSIVE": 1,
    "PRICE_LEVEL_MODERATE": 2,
    "PRICE_LEVEL_EXPENSIVE": 3,
    "PRICE_LEVEL_VERY_EXPENSIVE": 4,
}


def read_overrides() -> dict[str, dict]:
    if not config.OVERRIDES_FILE.exists():
        return {}
    with config.OVERRIDES_FILE.open() as f:
        return {row["place_id"]: row for row in csv.DictReader(f) if row.get("place_id")}


def main(argv: list[str] | None = None) -> None:
    places = config.read_jsonl(config.RESTAURANTS_FILE)
    statuses = {s["place_id"]: s["status"] for s in config.read_jsonl(config.MENU_STATUS_FILE)}
    prices = {p["place_id"]: p for p in config.read_jsonl(config.PRICES_FILE)}
    overrides = read_overrides()

    features, review = [], []
    for place in places:
        pid = place["place_id"]
        extraction = prices.get(pid, {})
        override = overrides.get(pid, {})
        if override.get("exclude", "").strip().lower() in ("1", "true", "yes"):
            continue

        price = extraction.get("benchmark_price")
        dish = next((i["dish_name"] for i in extraction.get("items", []) if i["price"] == price), None)
        confidence = extraction.get("confidence")
        source = "menu"
        if override.get("benchmark_price"):
            price, confidence, source = float(override["benchmark_price"]), "high", "manual"
            dish = override.get("dish_name") or dish
        if price is None:
            continue

        if not config.PRICE_MIN <= price <= config.PRICE_MAX and source != "manual":
            review.append({"place_id": pid, "name": place["name"], "price": price, "reason": "price out of range",
                           "source_url": extraction.get("source_url"), "notes": extraction.get("notes")})
            continue
        if confidence == "low":
            review.append({"place_id": pid, "name": place["name"], "price": price, "reason": "low confidence",
                           "source_url": extraction.get("source_url"), "notes": extraction.get("notes")})

        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(place["lng"], 6), round(place["lat"], 6)]},
                "properties": {
                    "id": pid,
                    "name": place["name"],
                    "address": place["address"],
                    "price": round(price, 2),
                    "dish": dish,
                    "items": extraction.get("items", []),
                    "confidence": confidence,
                    "price_source": source,
                    "source_url": extraction.get("source_url") or place.get("website"),
                    "website": place.get("website"),
                    "google_maps_url": place.get("google_maps_url"),
                    "price_level": PRICE_LEVELS.get(place.get("price_level")),
                    "rating": place.get("rating"),
                    "notes": override.get("note") or extraction.get("notes"),
                    "last_checked": (extraction.get("extracted_at") or "")[:10] or None,
                },
            }
        )

    features.sort(key=lambda f: f["properties"]["price"])
    config.GEOJSON_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.GEOJSON_FILE.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, indent=1)
    )

    with config.REVIEW_FILE.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["place_id", "name", "price", "reason", "source_url", "notes"])
        writer.writeheader()
        writer.writerows(review)

    status_counts: dict[str, int] = {}
    for s in statuses.values():
        status_counts[s] = status_counts.get(s, 0) + 1
    with_site = sum(1 for p in places if p.get("website"))
    has_calamari = sum(1 for p in prices.values() if p.get("has_calamari"))
    print("Funnel")
    print(f"  discovered          {len(places)}")
    print(f"  with website        {with_site}")
    print(f"  menu crawl          {', '.join(f'{k}={v}' for k, v in sorted(status_counts.items()))}")
    print(f"  sent to Claude      {len(prices)}")
    print(f"  has calamari        {has_calamari}")
    print(f"  on the map          {len(features)}")
    print(f"Wrote {config.GEOJSON_FILE.relative_to(config.ROOT)}; {len(review)} rows to review in "
          f"{config.REVIEW_FILE.relative_to(config.ROOT)}")


if __name__ == "__main__":
    main()
