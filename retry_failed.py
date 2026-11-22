"""
Retry city/category combinations that failed in earlier leads.py runs.

Reads "Discovery failed for <city> / <category>" lines from the given log files
and queries each OpenStreetMap tag of that category on its own (one small
request per tag), which the public Overpass servers handle far more reliably
than a whole category at once. New leads are appended to --output; websites
already present in any of the --existing CSVs are skipped.

Usage:
    python retry_failed.py --logs run_*.log --existing leads*.csv
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from pathlib import Path

import requests

import leads

FAIL_RE = re.compile(r"Discovery failed for (.+?) / (\w+):")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--logs", nargs="+", required=True)
    parser.add_argument("--existing", nargs="*", default=[])
    parser.add_argument("--output", default="leads_retry.csv")
    parser.add_argument("--max-per-city", type=int, default=15)
    parser.add_argument("--radius-km", type=float, default=8.0)
    parser.add_argument("--shard", default="1/1",
                        help="run only part i of n of the failures, e.g. 2/3")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    log = logging.getLogger("retry")

    failed: list[tuple[str, str]] = []
    for name in args.logs:
        for city, category in FAIL_RE.findall(Path(name).read_text()):
            if (city, category) not in failed and category in leads.CATEGORIES:
                failed.append((city, category))
    part, total = (int(x) for x in args.shard.split("/"))
    failed = failed[part - 1 :: total]
    log.info("%d failed combinations to retry", len(failed))

    output = Path(args.output)
    done: set[str] = set()
    for name in [*args.existing, args.output]:
        done |= leads.load_done(Path(name))

    session = requests.Session()
    session.headers["User-Agent"] = leads.USER_AGENT
    all_categories = dict(leads.CATEGORIES)
    written = 0
    for city, category in failed:
        found = 0
        for tag in all_categories[category]:
            if found >= args.max_per_city:
                break
            # Temporarily narrow the category to a single tag.
            leads.CATEGORIES[category] = [tag]
            try:
                batch = leads.discover(
                    city, [category], args.max_per_city - found, session,
                    args.radius_km,
                )
            finally:
                leads.CATEGORIES[category] = all_categories[category]
            for lead in batch:
                key = lead["website"].rstrip("/").lower()
                if key in done:
                    continue
                leads.append_row(output, lead)
                done.add(key)
                found += 1
                written += 1
        log.info("%s / %s: %d new leads", city, category, found)
        time.sleep(2)
    log.info("Done: %d new leads written to %s", written, output.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
