"""
Collect business leads from OpenStreetMap and enrich them with ScrapeGraphAI + Ollama.

Stage 1 (discover): for each city, query the OpenStreetMap Overpass API for
businesses in the chosen categories that list a website.
Stage 2 (enrich):   visit each business website with SmartScraperGraph running on a
local Ollama model and extract a public contact email, phone, services and a
short description.
Results are appended to a CSV after every lead, so a long run can be stopped and
resumed: leads already in the CSV are skipped.

Usage:
    python leads.py                                  # all categories, all cities
    python leads.py --categories real_estate --cities "Toronto,CA" "Leeds,GB"
    python leads.py --max-per-city 25 --no-enrich    # fast: OSM data only
    python leads.py --list-categories

Requirements: scrapegraphai installed, Playwright Chromium installed
(`playwright install chromium`), and Ollama running with the model pulled
(`ollama pull llama3.2:3b`).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
import urllib.robotparser
from pathlib import Path
from urllib.parse import urlparse

import requests
from pydantic import BaseModel, Field

USER_AGENT = "lead-research-script/0.1 (business contact research)"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

# OpenStreetMap tags per category. Edit freely: each entry is (key, value).
CATEGORIES: dict[str, list[tuple[str, str]]] = {
    "real_estate": [("office", "estate_agent"), ("shop", "estate_agent")],
    "healthcare": [
        ("amenity", "clinic"),
        ("amenity", "doctors"),
        ("amenity", "dentist"),
        ("healthcare", "physiotherapist"),
        ("healthcare", "psychotherapist"),
        ("healthcare", "alternative"),
    ],
    "consultants": [
        ("office", "consulting"),
        ("office", "financial_advisor"),
        ("office", "tax_advisor"),
        ("office", "accountant"),
        ("office", "lawyer"),
    ],
    # Coaches are sparse in OSM because many work online; these tags catch
    # tutoring/test-prep and training businesses with a physical location.
    "coaching": [
        ("amenity", "prep_school"),
        ("amenity", "training"),
        ("office", "educational_institution"),
        ("office", "coach"),
    ],
}

# "City,CountryCode". Add as many as you like, or pass --cities on the command line.
DEFAULT_CITIES = [
    # USA
    "New York,US", "Los Angeles,US", "Chicago,US", "Houston,US", "Miami,US",
    "Dallas,US", "San Francisco,US", "Seattle,US", "Boston,US", "Atlanta,US",
    # UK
    "London,GB", "Manchester,GB", "Birmingham,GB", "Leeds,GB", "Glasgow,GB",
    "Edinburgh,GB", "Bristol,GB",
    # Canada
    "Toronto,CA", "Vancouver,CA", "Montreal,CA", "Calgary,CA", "Ottawa,CA",
    # More
    "Sydney,AU", "Melbourne,AU", "Auckland,NZ", "Dublin,IE", "Dubai,AE",
    "Singapore,SG",
]

CSV_FIELDS = [
    "category", "name", "website", "email", "phone", "address", "city",
    "country", "services", "description", "osm_url", "enriched",
]

log = logging.getLogger("leads")


class LeadDetails(BaseModel):
    """Fields the LLM extracts from a business website."""

    email: str = Field(
        default="", description="Public business contact email, or empty if none"
    )
    phone: str = Field(default="", description="Business phone number, or empty")
    services: str = Field(
        default="", description="Main services offered, comma separated"
    )
    description: str = Field(
        default="", description="One sentence describing the business"
    )


# ----------------------------------------------------------------------------
# Stage 1: discover businesses on OpenStreetMap
# ----------------------------------------------------------------------------
def geocode_city(city: str, session: requests.Session) -> tuple[float, ...] | None:
    """Return (south, west, north, east) for a "City,CC" string via Nominatim."""
    name, _, country = city.partition(",")
    params = {"city": name.strip(), "format": "json", "limit": 1}
    if country.strip():
        params["countrycodes"] = country.strip().lower()
    resp = session.get(NOMINATIM_URL, params=params, timeout=30)
    resp.raise_for_status()
    time.sleep(1.1)  # Nominatim usage policy: max 1 request per second
    data = resp.json()
    if not data:
        return None
    s, n, w, e = (float(x) for x in data[0]["boundingbox"])
    return (s, w, n, e)


def overpass_query(query: str, session: requests.Session, rounds: int = 2) -> dict:
    """Run an Overpass query, trying each public server in turn.

    Public servers are often overloaded, so the whole list is tried ``rounds``
    times with a pause in between before giving up.
    """
    last_err: Exception | None = None
    for attempt in range(rounds):
        if attempt:
            log.info("All Overpass servers busy, retrying in 30 s")
            time.sleep(30)
        for url in OVERPASS_URLS:
            try:
                resp = session.post(url, data={"data": query}, timeout=200)
                if resp.status_code == 429:
                    log.warning("Overpass rate limit at %s", url)
                    continue
                resp.raise_for_status()
                return resp.json()
            except (requests.RequestException, ValueError) as err:
                log.warning("Overpass server %s failed: %s", url, err)
                last_err = err
    raise RuntimeError(f"All Overpass servers failed: {last_err}")


def _category_query(category: str, bbox_s: str, limit: int) -> str:
    """Overpass query for one category, returning at most ``limit`` elements."""
    selectors = "".join(
        f'nwr["{k}"="{v}"]["website"]({bbox_s});'
        f'nwr["{k}"="{v}"]["contact:website"]({bbox_s});'
        for k, v in CATEGORIES[category]
    )
    return f"[out:json][timeout:120];({selectors});out center tags {limit};"


def discover(
    city: str, categories: list[str], max_results: int, session: requests.Session
) -> list[dict]:
    """Return businesses with a website in one city, for all given categories.

    The city is geocoded once, then each category is queried separately with an
    output limit, which keeps each request small enough for the public Overpass
    servers. At most ``max_results`` leads are kept per category. A category whose
    query fails is logged and skipped; the others still return.
    """
    bbox = geocode_city(city, session)
    if bbox is None:
        log.warning("Could not geocode %s, skipping", city)
        return []
    bbox_s = ",".join(f"{x:.5f}" for x in bbox)
    elements = []
    for category in categories:
        # Ask for extra rows: some lack a name and are dropped below.
        query = _category_query(category, bbox_s, max_results * 2)
        try:
            elements.extend(overpass_query(query, session).get("elements", []))
        except RuntimeError as err:
            log.error("Discovery failed for %s / %s: %s", city, category, err)
        time.sleep(2)  # be gentle with Overpass between queries
    data = {"elements": elements}

    leads = []
    per_category: dict[str, int] = dict.fromkeys(categories, 0)
    seen: set[tuple[str, int]] = set()
    for el in data.get("elements", []):
        if (el["type"], el["id"]) in seen:
            continue
        seen.add((el["type"], el["id"]))
        tags = el.get("tags", {})
        category = next(
            (
                c
                for c in categories
                if any(tags.get(k) == v for k, v in CATEGORIES[c])
            ),
            None,
        )
        website = tags.get("website") or tags.get("contact:website") or ""
        name = tags.get("name", "")
        if category is None or not website or not name:
            continue
        if per_category[category] >= max_results:
            continue
        if not website.startswith(("http://", "https://")):
            website = "https://" + website
        address = " ".join(
            p
            for p in (
                tags.get("addr:housenumber", ""),
                tags.get("addr:street", ""),
                tags.get("addr:postcode", ""),
            )
            if p
        )
        leads.append(
            {
                "category": category,
                "name": name,
                "website": website,
                "email": tags.get("email") or tags.get("contact:email") or "",
                "phone": tags.get("phone") or tags.get("contact:phone") or "",
                "address": address,
                "city": tags.get("addr:city") or city.split(",")[0],
                "country": city.partition(",")[2].strip(),
                "services": "",
                "description": "",
                "osm_url": f"https://www.openstreetmap.org/{el['type']}/{el['id']}",
                "enriched": "no",
            }
        )
        per_category[category] += 1
    return leads


# ----------------------------------------------------------------------------
# Stage 2: enrich from the business's own website
# ----------------------------------------------------------------------------
_robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}


def allowed_by_robots(url: str) -> bool:
    """Respect robots.txt; if it cannot be read, allow."""
    parsed = urlparse(url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    if root not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        try:
            resp = requests.get(
                root + "/robots.txt", headers={"User-Agent": USER_AGENT}, timeout=15
            )
            rp.parse(resp.text.splitlines() if resp.ok else [])
            _robots_cache[root] = rp
        except requests.RequestException:
            _robots_cache[root] = None
    rp = _robots_cache[root]
    return rp is None or rp.can_fetch(USER_AGENT, url)


def enrich(lead: dict, graph_config: dict) -> dict:
    """Fill email/phone/services/description from the business website."""
    from scrapegraphai.graphs import SmartScraperGraph

    if not allowed_by_robots(lead["website"]):
        log.info("robots.txt disallows %s, keeping OSM data only", lead["website"])
        lead["enriched"] = "robots_disallowed"
        return lead

    graph = SmartScraperGraph(
        prompt=(
            f"This is the website of {lead['name']}, a {lead['category']} business. "
            "Extract the public business contact email, the phone number, the main "
            "services offered and a one-sentence description. Use empty strings "
            "for anything not on the page. Do not invent values."
        ),
        source=lead["website"],
        schema=LeadDetails,
        config=graph_config,
    )
    try:
        result = graph.run() or {}
    except Exception as err:  # one bad site must not stop a long run
        log.warning("Enrichment failed for %s: %s", lead["website"], err)
        lead["enriched"] = "failed"
        return lead

    for field in ("email", "phone", "services", "description"):
        value = result.get(field) if isinstance(result, dict) else None
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        if value and str(value).strip().upper() not in {"NA", "N/A", "NONE"}:
            # Prefer data already in OSM for email/phone; fill gaps from the site.
            if field in ("email", "phone") and lead[field]:
                continue
            lead[field] = str(value).strip()
    lead["enriched"] = "yes"
    return lead


# ----------------------------------------------------------------------------
# CSV helpers and main loop
# ----------------------------------------------------------------------------
def load_done(path: Path) -> set[str]:
    """Websites already written to the CSV (for resuming)."""
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8") as f:
        return {row["website"].rstrip("/").lower() for row in csv.DictReader(f)}


def append_row(path: Path, row: dict) -> None:
    new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--categories", nargs="+", default=list(CATEGORIES))
    parser.add_argument("--cities", nargs="+", default=DEFAULT_CITIES)
    parser.add_argument("--max-per-city", type=int, default=15,
                        help="max leads per city per category (default 15)")
    parser.add_argument("--output", default="leads.csv")
    parser.add_argument("--model", default=os.getenv("OLLAMA_MODEL", "llama3.2:3b"))
    parser.add_argument("--num-ctx", type=int, default=8192)
    parser.add_argument("--delay", type=float, default=3.0,
                        help="seconds to wait between websites (default 3)")
    parser.add_argument("--no-enrich", action="store_true",
                        help="skip website visits; write OpenStreetMap data only")
    parser.add_argument("--list-categories", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    if args.list_categories:
        print(json.dumps(CATEGORIES, indent=2))
        return 0
    unknown = set(args.categories) - set(CATEGORIES)
    if unknown:
        parser.error(f"unknown categories: {', '.join(sorted(unknown))}")

    graph_config = {
        "llm": {
            "model": f"ollama/{args.model}",
            "temperature": 0,
            "format": "json",
            "model_tokens": args.num_ctx,
            "num_ctx": args.num_ctx,  # Ollama's default context is too small
        },
        "headless": True,
        "verbose": False,
    }

    output = Path(args.output)
    done = load_done(output)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    written = 0

    for city in args.cities:
        try:
            leads = discover(city, args.categories, args.max_per_city, session)
        except Exception as err:
            log.error("Discovery failed for %s: %s", city, err)
            continue
        log.info("%s: %d businesses with a website", city, len(leads))
        for lead in leads:
            key = lead["website"].rstrip("/").lower()
            if key in done:
                continue
            if not args.no_enrich:
                lead = enrich(lead, graph_config)
                time.sleep(args.delay)
            append_row(output, lead)
            done.add(key)
            written += 1
            log.info("[%d] %s | %s | %s | %s", written, lead["category"],
                     lead["name"], lead["email"] or "-", lead["website"])

    log.info("Done: %d new leads written to %s", written, output.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
