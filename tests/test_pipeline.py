import json

import pytest

from pipeline import build, config, discover, extract, fetch_menus


def test_grid_cells_cover_bbox():
    cells = discover.grid_cells(2, 3)
    south, west, north, east = config.BBOX
    assert len(cells) == 6
    assert cells[0]["low"] == {"latitude": south, "longitude": west}
    assert cells[-1]["high"]["latitude"] == pytest.approx(north)
    assert cells[-1]["high"]["longitude"] == pytest.approx(east)


def test_locality():
    place = {"addressComponents": [{"longText": "Burnaby", "types": ["locality", "political"]}]}
    assert discover.locality(place) == "Burnaby"
    assert discover.locality({}) is None


def test_menu_links_same_domain_and_pdf_first():
    html = """
      <a href="/about">About</a>
      <a href="/menus/dinner">Dinner</a>
      <a href="https://www.example.com/files/food-menu.pdf">Download</a>
      <a href="https://instagram.com/menu">Insta</a>
      <a href="https://example.popmenu.com/menu">Order</a>
      <img src="/img/happy-menu.jpg" alt="Our menu">
      <img src="data:image/svg+xml;base64,AAAA" alt="menu">
      <iframe src="https://menu.example.com/food"></iframe>
      <a href="/menus/dinner/">Dinner again</a>
    """
    pages, images = fetch_menus.menu_links(html, "https://example.com/")
    assert pages[0].endswith("food-menu.pdf")
    assert "https://example.com/menus/dinner" in pages
    assert "https://example.popmenu.com/menu" in pages
    assert "https://menu.example.com/food" in pages  # embedded menu on a subdomain
    assert pages.count("https://example.com/menus/dinner") == 1
    assert not any("instagram" in p or "about" in p for p in pages)
    assert images == ["https://example.com/img/happy-menu.jpg"]


def test_price_pattern_ignores_addresses():
    text = "Crispy Calamari 18\nWings $16\nBurger 21.50\n1055 Canada Pl #36\n(604) 647-7513\nMay 2026"
    assert len(fetch_menus.PRICE.findall(text)) == 3


def test_html_text_drops_scripts():
    text = fetch_menus.html_text("<html><body><script>var x=1</script><p>Calamari 18</p></body></html>")
    assert text == "Calamari 18"


@pytest.mark.parametrize(
    "sources, expected",
    [
        ([{"error": "HTTP 404"}], "fetch_failed"),
        ([{"kind": "html", "text_chars": 900, "mentions": 0}, {"kind": "html", "text_chars": 500, "mentions": 2}], "mentions"),
        ([{"kind": "html", "text_chars": 900, "mentions": 0}, {"kind": "pdf", "text_chars": 10, "mentions": 0}], "needs_vision"),
        ([{"kind": "html", "text_chars": 1000, "mentions": 0, "prices": 2}], "menu_not_found"),
        ([{"kind": "html", "text_chars": 900, "mentions": 0, "prices": 30}], "no_mention"),
    ],
)
def test_classify(sources, expected):
    assert fetch_menus.classify(sources) == expected


def test_snippets_merge_overlapping_windows():
    text = "A" * 5000 + "Calamari 17" + "B" * 100 + "squid 9" + "C" * 5000
    out = extract.snippets(text)
    assert "[...]" not in out  # both mentions fall in one window
    assert "Calamari 17" in out and "squid 9" in out
    assert len(out) < 2 * extract.SNIPPET_RADIUS + 200


def _setup(tmp_path, monkeypatch):
    for name in ["RESTAURANTS_FILE", "MENU_STATUS_FILE", "PRICES_FILE", "OVERRIDES_FILE", "REVIEW_FILE", "GEOJSON_FILE"]:
        monkeypatch.setattr(config, name, tmp_path / getattr(config, name).name)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "MENUS_DIR", tmp_path / "menus")


def test_build_content_and_hash_stable(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    d = config.MENUS_DIR / "p1"
    d.mkdir(parents=True)
    (d / "01.txt").write_text("Starters\nCrispy Calamari 18\nWings 16")
    (d / "manifest.json").write_text(json.dumps({"sources": [
        {"url": "https://x.com", "kind": "html", "file": "00.html", "text_file": "00.txt", "text_chars": 0, "mentions": 0},
        {"url": "https://x.com/menu", "kind": "html", "file": "01.html", "text_file": "01.txt", "text_chars": 40, "mentions": 1},
    ]}))
    place = {"place_id": "p1", "name": "X", "address": "1 Main St", "website": "https://x.com"}
    blocks, h1 = extract.build_content(place, "mentions")
    _, h2 = extract.build_content(place, "mentions")
    assert h1 == h2
    assert "Crispy Calamari 18" in blocks[-1]["text"]
    assert 'url="https://x.com/menu"' in blocks[-1]["text"]


def test_build_applies_overrides_and_range(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    places = [
        {"place_id": pid, "name": pid, "lat": 49.28, "lng": -123.12, "address": "a", "website": None,
         "google_maps_url": None, "price_level": "PRICE_LEVEL_MODERATE", "rating": 4.5}
        for pid in ("ok", "huge", "manual", "excluded", "none")
    ]
    item = lambda p: [{"dish_name": "Calamari", "price": p, "portion_note": None, "is_happy_hour": False}]
    prices = [
        {"place_id": "ok", "benchmark_price": 18.0, "items": item(18.0), "confidence": "low", "has_calamari": True},
        {"place_id": "huge", "benchmark_price": 180.0, "items": item(180.0), "confidence": "high", "has_calamari": True},
        {"place_id": "manual", "benchmark_price": None, "items": [], "confidence": "low", "has_calamari": True},
        {"place_id": "excluded", "benchmark_price": 20.0, "items": item(20.0), "confidence": "high", "has_calamari": True},
    ]
    config.write_jsonl(config.RESTAURANTS_FILE, places)
    config.write_jsonl(config.PRICES_FILE, prices)
    config.OVERRIDES_FILE.write_text(
        "place_id,benchmark_price,dish_name,exclude,note\nmanual,21,Fried Squid,,checked in person\nexcluded,,,true,\n"
    )
    build.main([])
    feats = {f["properties"]["id"]: f["properties"] for f in json.loads(config.GEOJSON_FILE.read_text())["features"]}
    assert set(feats) == {"ok", "manual"}
    assert feats["ok"]["dish"] == "Calamari" and feats["ok"]["price_level"] == 2
    assert feats["manual"]["price"] == 21 and feats["manual"]["dish"] == "Fried Squid"
    review = config.REVIEW_FILE.read_text()
    assert "huge" in review and "price out of range" in review and "low confidence" in review
