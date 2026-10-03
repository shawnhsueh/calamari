"""Step 2: crawl restaurant websites for menus and pre-filter for calamari mentions.

For every restaurant this writes data/menus/<place_id>/ (raw files + manifest.json)
and one line in data/menu_status.jsonl with a status:

  no_website    Google has no website for the place
  fetch_failed  homepage could not be fetched
  menu_not_found  no page with menu prices found (often a JavaScript-rendered menu)
  no_mention    menu with prices found, but no calamari/squid in it
  mentions      menu text mentions calamari/squid -> sent to Claude
  needs_vision  only image/scanned-PDF menus found -> sent to Claude as images/PDFs
"""

import argparse
import asyncio
import io
import json
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx
from pypdf import PdfReader
from selectolax.lexbor import LexborHTMLParser as HTMLParser
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from . import config

CONCURRENCY = 8
MAX_MENU_LINKS = 5
MAX_BYTES = 15 * 1024 * 1024
MIN_TEXT_CHARS = 200
MIN_PRICES = 8
MAX_NESTED = 3

MENU_WORDS = re.compile(r"menu|food|dinner|lunch|brunch|happy[\s_-]?hour|eat|appetizer|share", re.I)
MENU_HOSTS = ("popmenu", "toasttab", "menufy", "squarespace", "wixstatic", "bentobox", "getbento", "singleplatform", "order.online")
MENTION = re.compile(config.CALAMARI_PATTERN, re.I)
# "$18", "18.50", or a bare "18" ending a line (common menu layouts).
PRICE = re.compile(r"\$\s?\d{1,3}(?:\.\d{2})?|\b\d{1,3}\.\d{2}\b|(?:^|[ \t])\d{1,2}(?:\.\d)?[ \t]*$", re.M)
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp")


class Retryable(Exception):
    pass


@retry(
    retry=retry_if_exception_type((Retryable, httpx.TransportError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=1, max=10),
)
async def get(client: httpx.AsyncClient, url: str) -> httpx.Response:
    resp = await client.get(url)
    if resp.status_code in (429, 500, 502, 503, 504):
        raise Retryable(f"{resp.status_code} {url}")
    return resp


def html_text(html: str) -> str:
    tree = HTMLParser(html)
    for tag in tree.css("script, style, noscript, svg, iframe"):
        tag.decompose()
    body = tree.body or tree.root
    text = body.text(separator="\n") if body else ""
    lines = (re.sub(r"\s+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def pdf_text(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except Exception:
        return ""


def site_of(url: str) -> str:
    """Registrable domain, roughly: menu.joes.ca and www.joes.ca are both joes.ca."""
    return ".".join(urlparse(url).netloc.lower().split(":")[0].split(".")[-2:])


def normalize(url: str) -> str:
    return url.split("#")[0].rstrip("/")


def resolve(base_url: str, href: str) -> str | None:
    href = (href or "").strip()
    if not href:
        return None
    url = urljoin(base_url, href)
    return normalize(url) if urlparse(url).scheme in ("http", "https") else None


def menu_links(html: str, base_url: str) -> tuple[list[str], list[str]]:
    """Return (menu pages/PDFs/embedded frames, menu images) linked from a page."""
    tree = HTMLParser(html)
    base_site = site_of(base_url)
    trusted = lambda url: site_of(url) == base_site or any(h in urlparse(url).netloc for h in MENU_HOSTS)
    pages: list[str] = []
    images: list[str] = []
    # Embedded menus (iframes) count regardless of their wording.
    for frame in tree.css("iframe[src]"):
        url = resolve(base_url, frame.attributes.get("src"))
        if url and trusted(url):
            pages.append(url)
    for a in tree.css("a[href]"):
        url = resolve(base_url, a.attributes.get("href"))
        if not url or not trusted(url) or not MENU_WORDS.search(f"{a.text(strip=True)} {url}"):
            continue
        (images if url.lower().endswith(IMAGE_EXT) else pages).append(url)
    for img in tree.css("img"):
        url = resolve(base_url, img.attributes.get("src") or img.attributes.get("data-src"))
        if url and re.search(r"menu", f"{img.attributes.get('alt') or ''} {url}", re.I):
            images.append(url)
    pages = [u for u in dict.fromkeys(pages) if u != normalize(base_url)]
    # PDFs first: they are usually the full menu.
    pages.sort(key=lambda u: not u.lower().endswith(".pdf"))
    return pages[:MAX_MENU_LINKS], list(dict.fromkeys(images))[:3]


def kind_of(resp: httpx.Response, url: str) -> str:
    ctype = resp.headers.get("content-type", "").lower()
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        return "pdf"
    if ctype.startswith("image/") or url.lower().endswith(IMAGE_EXT):
        return "image"
    return "html"


async def fetch_source(client: httpx.AsyncClient, url: str, out_dir, index: int) -> dict | None:
    try:
        resp = await get(client, url)
    except Exception as e:
        return {"url": url, "error": str(e)[:200]}
    if resp.status_code != 200:
        return {"url": url, "error": f"HTTP {resp.status_code}"}
    if len(resp.content) > MAX_BYTES:
        return {"url": url, "error": "too large"}
    kind = kind_of(resp, url)
    ext = {"pdf": ".pdf", "image": ".img", "html": ".html"}[kind]
    raw_file = out_dir / f"{index:02d}{ext}"
    raw_file.write_bytes(resp.content)
    source = {
        "url": str(resp.url),
        "kind": kind,
        "file": raw_file.name,
        "media_type": resp.headers.get("content-type", "").split(";")[0].strip(),
    }
    if kind == "image":
        source["text_chars"] = 0
        return source
    text = pdf_text(resp.content) if kind == "pdf" else html_text(resp.text)
    (out_dir / f"{index:02d}.txt").write_text(text)
    source["text_file"] = f"{index:02d}.txt"
    source["text_chars"] = len(text)
    source["mentions"] = len(MENTION.findall(text))
    source["prices"] = len(PRICE.findall(text))
    if kind == "html":
        source["_html"] = resp.text
    return source


def classify(sources: list[dict]) -> str:
    ok = [s for s in sources if "error" not in s]
    if not ok or "error" in sources[0]:
        return "fetch_failed"
    if any(s.get("mentions") for s in ok):
        return "mentions"
    scanned_pdfs = [s for s in ok if s["kind"] == "pdf" and s["text_chars"] < MIN_TEXT_CHARS]
    images = [s for s in ok if s["kind"] == "image"]
    if scanned_pdfs or images:
        return "needs_vision"
    # Without a handful of prices we never actually saw a menu (often it's rendered by JavaScript).
    if sum(s.get("prices", 0) for s in ok) < MIN_PRICES:
        return "menu_not_found"
    return "no_mention"


def reparse(statuses: list[dict]) -> list[dict]:
    """Re-extract text from already-downloaded PDFs (no network) and re-classify."""
    changed = 0
    for status in statuses:
        out_dir = config.MENUS_DIR / status["place_id"]
        manifest_file = out_dir / "manifest.json"
        if not manifest_file.exists():
            continue
        manifest = json.loads(manifest_file.read_text())
        for src in manifest["sources"]:
            if src.get("kind") != "pdf" or not (out_dir / src["file"]).exists():
                continue
            text = pdf_text((out_dir / src["file"]).read_bytes())
            (out_dir / src["text_file"]).write_text(text)
            src.update(text_chars=len(text), mentions=len(MENTION.findall(text)), prices=len(PRICE.findall(text)))
        manifest_file.write_text(json.dumps(manifest, indent=2))
        new_status = classify(manifest["sources"])
        if new_status != status["status"]:
            print(f"  {status['place_id']}: {status['status']} -> {new_status}")
            status["status"] = new_status
            changed += 1
    print(f"Re-parsed saved PDFs; {changed} restaurants changed status")
    return statuses


async def crawl(client: httpx.AsyncClient, restaurant: dict) -> dict:
    out_dir = config.MENUS_DIR / restaurant["place_id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    home_url = restaurant["website"]
    home = await fetch_source(client, home_url, out_dir, 0)
    sources = [home]
    if home and "error" not in home and home["kind"] == "html":
        pages, images = menu_links(home.pop("_html"), home["url"])
        targets = pages + images
        sources += await asyncio.gather(*(fetch_source(client, u, out_dir, i) for i, u in enumerate(targets, 1)))
        # One level deeper for menu hubs (a "Menus" page linking to PDFs or embedding the real menu).
        seen = {normalize(u) for u in targets} | {normalize(home_url), normalize(home["url"])}
        nested = []
        for src in sources[1:]:
            html = src.pop("_html", None)
            if html and not src.get("mentions"):
                for url in menu_links(html, src["url"])[0]:
                    if url not in seen:
                        seen.add(url)
                        nested.append(url)
        nested.sort(key=lambda u: not u.lower().endswith(".pdf"))
        start = len(targets) + 1
        sources += await asyncio.gather(
            *(fetch_source(client, u, out_dir, i) for i, u in enumerate(nested[:MAX_NESTED], start))
        )
    # Different links can redirect to the same page; keep the first copy.
    unique, seen_final = [], set()
    for s in sources:
        s.pop("_html", None)
        if normalize(s["url"]) not in seen_final or "error" in s:
            seen_final.add(normalize(s["url"]))
            unique.append(s)
    sources = unique
    manifest = {
        "place_id": restaurant["place_id"],
        "website": home_url,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": sources,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return {"place_id": restaurant["place_id"], "status": classify(sources), "fetched_at": manifest["fetched_at"]}


async def run(restaurants: list[dict], refresh: bool) -> list[dict]:
    statuses = {s["place_id"]: s for s in config.read_jsonl(config.MENU_STATUS_FILE)}
    todo = []
    for r in restaurants:
        if not r.get("website"):
            statuses[r["place_id"]] = {"place_id": r["place_id"], "status": "no_website"}
        elif refresh or r["place_id"] not in statuses or statuses[r["place_id"]]["status"] == "no_website":
            todo.append(r)
    print(f"Crawling {len(todo)} websites ({len(restaurants) - len(todo)} cached or without a website)")

    sem = asyncio.Semaphore(CONCURRENCY)
    done = 0

    async def worker(client, r):
        nonlocal done
        async with sem:
            try:
                status = await asyncio.wait_for(crawl(client, r), timeout=120)
            except Exception as e:
                status = {"place_id": r["place_id"], "status": "fetch_failed", "error": str(e)[:200]}
        statuses[r["place_id"]] = status
        done += 1
        if done % 25 == 0:
            print(f"  {done}/{len(todo)}")

    async with httpx.AsyncClient(
        headers={"User-Agent": config.USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8", "Accept-Language": "en-CA,en;q=0.9"}, timeout=15, follow_redirects=True
    ) as client:
        await asyncio.gather(*(worker(client, r) for r in todo))
    return list(statuses.values())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, help="only crawl the first N restaurants")
    parser.add_argument("--refresh", action="store_true", help="re-crawl sites that were already fetched")
    parser.add_argument("--reparse", action="store_true", help="re-read text from saved PDFs without re-crawling")
    args = parser.parse_args(argv)

    if args.reparse:
        statuses = reparse(config.read_jsonl(config.MENU_STATUS_FILE))
        config.write_jsonl(config.MENU_STATUS_FILE, statuses)
        return

    restaurants = config.read_jsonl(config.RESTAURANTS_FILE)
    if not restaurants:
        raise SystemExit("No restaurants yet. Run `python -m pipeline.discover` first.")
    if args.limit:
        restaurants = restaurants[: args.limit]
    statuses = asyncio.run(run(restaurants, args.refresh))
    config.write_jsonl(config.MENU_STATUS_FILE, sorted(statuses, key=lambda s: s["place_id"]))

    counts: dict[str, int] = {}
    for s in statuses:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    print("Menu status:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()
