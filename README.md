# Calamari Index — Vancouver

**Live map: https://shawnhsueh.github.io/calamari/**

How pricey is a restaurant? Calamari is on nearly every shared-plates menu, so its price is a
handy benchmark. This project finds the calamari price at restaurants in the City of Vancouver
and plots them on an interactive map.

```
Google Places ──► restaurant websites ──► Claude reads the menu ──► GeoJSON ──► Leaflet map
 discover.py        fetch_menus.py            extract.py             build.py      web/
```

## Setup

```sh
conda create -n calamari python=3.12 && conda activate calamari
pip install -e '.[dev]'
cp .env.example .env   # add GOOGLE_MAPS_API_KEY (Places API (New) enabled) and ANTHROPIC_API_KEY
```

## Run

```sh
python -m pipeline.run                 # everything (extraction runs as a Message Batch, ~<1h)
python -m pipeline.run --sync          # same, but extraction one request at a time

# or step by step
python -m pipeline.discover --limit-cells 1   # try one grid cell first
python -m pipeline.fetch_menus --limit 20
python -m pipeline.extract --dry-run          # see what would be sent and roughly how many tokens
python -m pipeline.extract --sync --limit 10
python -m pipeline.build

python -m http.server -d web 8000             # open http://localhost:8000
```

Every step is cached and incremental: Places responses are cached in `data/cache/`, crawled
menus live in `data/menus/<place_id>/`, and a menu is only re-sent to Claude when its content
changes. Use `fetch_menus --refresh` to re-crawl and `extract --force` to re-extract.

## How prices are chosen

- `fetch_menus` finds menu pages, PDFs, embedded menus (iframes) and menu images on each site, and
  only restaurants whose menus mention *calamari/squid*, or that only have scanned/image menus, are sent to
  Claude (`claude-sonnet-5-5`, structured outputs).
- The **benchmark price** is the regular-menu calamari appetizer (smallest share size, never
  happy hour). All calamari items found are kept and shown in the popup.
- `build` keeps prices between $8 and $45; anything else, plus low-confidence reads, goes
  to `data/review.csv`.

## Fixing data by hand

Edit `data/overrides.csv` and re-run `python -m pipeline.build`:

```csv
place_id,benchmark_price,dish_name,exclude,note
ChIJ...,19,Crispy Calamari,,checked in person 2026-10
ChIJ...,,,true,closed
```

## Known gaps

- Menus that need JavaScript to render, or sites that block crawlers, end up as
  `menu_not_found` / `fetch_failed` in `data/menu_status.jsonl` (some big chains, e.g. Cactus Club, return 403).
  These are the best candidates for manual overrides.
- Chains often post one menu for all locations, so every location shows the same price.

## Updating the site

`web/` is a static site deployed to GitHub Pages by `.github/workflows/pages.yml` on every push
that touches `web/`. To refresh the data: re-run the pipeline, then commit and push
`web/data/restaurants.geojson`.

## Data & attribution

- Restaurant names and locations: Google Places API.
- Calamari prices: read from each restaurant's own website/menus (last run: 2026-10-03). Prices
  change; check the linked menu before you go.
- Basemap tiles: © Esri — Esri, HERE, Garmin, © OpenStreetMap contributors.

A personal, non-commercial project. Not affiliated with any of the restaurants listed.

## Tests

```sh
pytest
```
