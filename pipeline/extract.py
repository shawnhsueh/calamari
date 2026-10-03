"""Step 3: read the calamari price off each menu with Claude.

Restaurants whose menus mention calamari/squid (or only have image/scanned menus) are
sent to Claude with structured outputs. By default the run uses the Message Batches
API (half price, usually done within an hour); --sync sends requests one at a time.
"""

import argparse
import base64
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from typing import Literal

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request
from pydantic import BaseModel, Field, ValidationError

from . import config


class CalamariItem(BaseModel):
    dish_name: str = Field(description='Name as printed, e.g. "Crispy Calamari", "Salt & Pepper Squid"')
    price: float = Field(description="Price in CAD as printed on the menu")
    portion_note: str | None = Field(description='Size or context, e.g. "small", "share plate", "happy hour"')
    is_happy_hour: bool


class Extraction(BaseModel):
    has_calamari: bool = Field(description="True if the menu has at least one calamari/squid dish")
    items: list[CalamariItem]
    benchmark_price: float | None = Field(
        description="Regular-menu (not happy hour) calamari appetizer price; smallest share size if several. "
        "Null if no price is printed."
    )
    source_url: str | None = Field(description="URL of the menu source the benchmark price came from")
    confidence: Literal["high", "medium", "low"]
    notes: str | None = Field(description="Anything a reviewer should know, e.g. 'market price', 'menu may be outdated'")


SYSTEM = """You extract calamari prices from restaurant menus in Vancouver, BC, for a price-comparison map.

What counts: dishes whose main ingredient is squid or calamari (fried, grilled, salt & pepper squid, \
calamari fritti, kalamarakia, ika, etc.), as an appetizer, share plate, or main. Don't count dishes \
where squid is one of several ingredients (paella, seafood platters, fritto misto with other seafood).

benchmark_price is the one number that represents the restaurant on the map:
- Prefer the regular-menu appetizer/share-plate calamari.
- If there are several sizes, use the smallest share size. If there are several calamari dishes, \
use the most standard fried calamari appetizer.
- Never use happy-hour or brunch-special prices for the benchmark; list them in items instead.
- Use only prices printed on the menu. If the price is missing, "market price", or unreadable, set \
benchmark_price to null. Never estimate.

Menu text was scraped from websites and may be a fragment, out of order, or include unrelated pages. \
Set confidence to "low" if you can't tell which price belongs to the dish, "medium" if the menu \
looks partial or possibly outdated, otherwise "high"."""

SNIPPET_RADIUS = 1200
MAX_TEXT_CHARS = 14000
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024


def snippets(text: str) -> str:
    """Windows of text around every calamari mention, merged where they overlap."""
    spans: list[list[int]] = []
    for m in re.finditer(config.CALAMARI_PATTERN, text, re.I):
        start, end = max(0, m.start() - SNIPPET_RADIUS), min(len(text), m.end() + SNIPPET_RADIUS)
        if spans and start <= spans[-1][1]:
            spans[-1][1] = end
        else:
            spans.append([start, end])
    return "\n[...]\n".join(text[s:e] for s, e in spans)


def build_content(place: dict, status: str) -> tuple[list[dict], str] | None:
    """Return (message content blocks, content hash), or None if there's nothing to send."""
    out_dir = config.MENUS_DIR / place["place_id"]
    manifest_file = out_dir / "manifest.json"
    if not manifest_file.exists():
        return None
    manifest = json.loads(manifest_file.read_text())
    blocks: list[dict] = []
    text_parts: list[str] = []
    budget = MAX_TEXT_CHARS
    for src in manifest["sources"]:
        if "error" in src:
            continue
        if src.get("mentions") and budget > 0:
            part = snippets((out_dir / src["text_file"]).read_text())[:budget]
            budget -= len(part)
            text_parts.append(f"<source url=\"{src['url']}\">\n{part}\n</source>")
        elif status == "needs_vision" and src["kind"] in ("pdf", "image"):
            data = (out_dir / src["file"]).read_bytes()
            if len(data) > MAX_ATTACHMENT_BYTES:
                continue
            b64 = base64.standard_b64encode(data).decode()
            if src["kind"] == "pdf":
                blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}})
            elif src.get("media_type") in IMAGE_TYPES:
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": src["media_type"], "data": b64}})
            else:
                continue
            text_parts.append(f"(Attachment {len(blocks)} is from {src['url']})")
    if not blocks and not any(p.startswith("<source") for p in text_parts):
        return None
    intro = f"Restaurant: {place['name']}\nAddress: {place['address']}\nWebsite: {place.get('website')}\n\n"
    blocks.append({"type": "text", "text": intro + "\n\n".join(text_parts)})
    digest = hashlib.sha256(json.dumps(blocks, sort_keys=True).encode()).hexdigest()[:16]
    return blocks, digest


def params(content: list[dict]) -> dict:
    return {
        "model": config.MODEL,
        "max_tokens": 8000,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": content}],
    }


def record(place_id: str, digest: str, extraction: Extraction | None, status: str, error: str | None = None) -> dict:
    row = {
        "place_id": place_id,
        "content_hash": digest,
        "status": status,
        "model": config.MODEL,
        "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if extraction:
        row.update(extraction.model_dump())
    if error:
        row["error"] = error
    return row


def extract_sync(client: anthropic.Anthropic, place_id: str, content: list[dict], digest: str) -> dict:
    try:
        resp = client.beta.messages.parse(
            **params(content),
            output_format=Extraction,
            output_config={"effort": "low"},
            betas=[config.FALLBACK_BETA],
            fallbacks="default",
        )
    except anthropic.BadRequestError as e:
        return record(place_id, digest, None, "error", f"bad request: {e.message}"[:300])
    except anthropic.APIStatusError as e:
        return record(place_id, digest, None, "error", f"{e.status_code}: {e.message}"[:300])
    if resp.stop_reason == "refusal":
        return record(place_id, digest, None, "refused")
    if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
        return record(place_id, digest, None, "error", f"no parsed output (stop_reason={resp.stop_reason})")
    return record(place_id, digest, resp.parsed_output, "ok")


def submit_batch(client: anthropic.Anthropic, jobs: dict[str, tuple[list[dict], str]]) -> str:
    # Server-side fallbacks aren't accepted by the Batches API; refusals are retried with --sync below.
    requests = [
        Request(
            custom_id=place_id,
            params=MessageCreateParamsNonStreaming(
                **params(content),
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": anthropic.transform_schema(Extraction)},
                },
            ),
        )
        for place_id, (content, _) in jobs.items()
    ]
    batch = client.messages.batches.create(requests=requests)
    config.BATCH_STATE_FILE.write_text(
        json.dumps({"batch_id": batch.id, "hashes": {pid: d for pid, (_, d) in jobs.items()}})
    )
    print(f"Submitted batch {batch.id} with {len(requests)} requests")
    return batch.id


def collect_batch(client: anthropic.Anthropic, batch_id: str, hashes: dict[str, str]) -> list[dict]:
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        if batch.processing_status == "ended":
            break
        c = batch.request_counts
        print(f"  batch {batch.processing_status}: {c.processing} processing, {c.succeeded} done")
        time.sleep(60)
    rows = []
    for result in client.messages.batches.results(batch_id):
        pid, digest = result.custom_id, hashes[result.custom_id]
        if result.result.type != "succeeded":
            rows.append(record(pid, digest, None, "error", f"batch result {result.result.type}"))
            continue
        msg = result.result.message
        if msg.stop_reason == "refusal":
            rows.append(record(pid, digest, None, "refused"))
            continue
        text = next((b.text for b in msg.content if b.type == "text"), "")
        try:
            rows.append(record(pid, digest, Extraction.model_validate_json(text), "ok"))
        except ValidationError as e:
            rows.append(record(pid, digest, None, "error", f"invalid JSON: {e}"[:300]))
    config.BATCH_STATE_FILE.unlink(missing_ok=True)
    return rows


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sync", action="store_true", help="send requests one at a time instead of as a batch")
    parser.add_argument("--limit", type=int, help="only process the first N candidates")
    parser.add_argument("--force", action="store_true", help="re-extract even if the menu content is unchanged")
    parser.add_argument("--dry-run", action="store_true", help="show what would be sent, without calling Claude")
    args = parser.parse_args(argv)

    places = {r["place_id"]: r for r in config.read_jsonl(config.RESTAURANTS_FILE)}
    statuses = config.read_jsonl(config.MENU_STATUS_FILE)
    prices = {p["place_id"]: p for p in config.read_jsonl(config.PRICES_FILE)}

    jobs: dict[str, tuple[list[dict], str]] = {}
    for s in statuses:
        if s["status"] not in ("mentions", "needs_vision") or s["place_id"] not in places:
            continue
        built = build_content(places[s["place_id"]], s["status"])
        if not built:
            continue
        prev = prices.get(s["place_id"])
        if not args.force and prev and prev["content_hash"] == built[1] and prev["status"] == "ok":
            continue
        jobs[s["place_id"]] = built
    if args.limit:
        jobs = dict(list(jobs.items())[: args.limit])

    if args.dry_run:
        chars = sum(len(json.dumps(c)) for c, _ in jobs.values())
        print(f"{len(jobs)} restaurants to extract, ~{chars // 4:,} input tokens (rough)")
        for pid, (content, _) in list(jobs.items())[:3]:
            print(f"--- {places[pid]['name']}\n{content[-1]['text'][:1500]}\n")
        return

    client = anthropic.Anthropic(api_key=config.require_env("ANTHROPIC_API_KEY"))

    # Resume a batch left over from an interrupted run.
    if config.BATCH_STATE_FILE.exists():
        state = json.loads(config.BATCH_STATE_FILE.read_text())
        print(f"Resuming batch {state['batch_id']}")
        new_rows = collect_batch(client, state["batch_id"], state["hashes"])
    elif not jobs:
        print("Nothing to extract: every candidate menu is unchanged since the last run.")
        return
    elif args.sync:
        new_rows = []
        for i, (pid, (content, digest)) in enumerate(jobs.items(), 1):
            row = extract_sync(client, pid, content, digest)
            print(f"[{i}/{len(jobs)}] {places[pid]['name']}: {row['status']} {row.get('benchmark_price')}")
            new_rows.append(row)
    else:
        batch_id = submit_batch(client, jobs)
        hashes = {pid: d for pid, (_, d) in jobs.items()}
        new_rows = collect_batch(client, batch_id, hashes)

    # Refusals and errors from the batch get one more try with server-side fallbacks.
    retry_ids = [r["place_id"] for r in new_rows if r["status"] in ("refused", "error") and r["place_id"] in jobs]
    if retry_ids and not args.sync:
        print(f"Retrying {len(retry_ids)} failed requests one at a time")
        by_id = {r["place_id"]: r for r in new_rows}
        for pid in retry_ids:
            content, digest = jobs[pid]
            by_id[pid] = extract_sync(client, pid, content, digest)
        new_rows = list(by_id.values())

    for row in new_rows:
        prices[row["place_id"]] = row
    config.write_jsonl(config.PRICES_FILE, sorted(prices.values(), key=lambda r: r["place_id"]))
    ok = [r for r in new_rows if r["status"] == "ok"]
    priced = [r for r in ok if r.get("benchmark_price") is not None]
    print(f"Extracted {len(new_rows)}: {len(ok)} ok, {len(priced)} with a benchmark price -> {config.PRICES_FILE}")


if __name__ == "__main__":
    main()
