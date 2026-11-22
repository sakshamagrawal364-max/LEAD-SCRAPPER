"""
Find public contact emails for leads in a CSV produced by leads.py.

For each lead without an email, fetch the homepage and a few common contact
pages, collect addresses from mailto: links and visible text, and keep the best
one (an address on the business's own domain is preferred). Respects
robots.txt and waits between sites. No LLM needed.

Usage:
    python find_emails.py                       # leads.csv -> leads_with_email.csv
    python find_emails.py --input leads.csv --output leads_with_email.csv
    python find_emails.py --all                 # also keep leads without an email
"""

from __future__ import annotations

import argparse
import csv
import html
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests

from leads import CSV_FIELDS, USER_AGENT, allowed_by_robots

log = logging.getLogger("find_emails")

CONTACT_PATHS = ["", "/contact", "/contact-us", "/contact/", "/about", "/about-us"]
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}")
MAILTO_RE = re.compile(r'mailto:([^"\'?>\s]+)', re.IGNORECASE)
JUNK_DOMAINS = {
    "example.com", "domain.com", "email.com", "sentry.io", "wixpress.com",
    "sentry-next.wixpress.com", "godaddy.com", "yourdomain.com", "mysite.com",
}
JUNK_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".js", ".css")


def _clean(email: str) -> str | None:
    email = unquote(html.unescape(email)).strip().strip(".").lower()
    if not EMAIL_RE.fullmatch(email):
        return None
    domain = email.split("@", 1)[1]
    if domain in JUNK_DOMAINS or email.endswith(JUNK_SUFFIXES):
        return None
    if re.search(r"\d+x\d*\.", email):  # e.g. logo@2x.png
        return None
    return email


def _site_domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def find_email(website: str, session: requests.Session) -> tuple[str, str]:
    """Return (best public email, page it was found on), or ('', '')."""
    domain = _site_domain(website)
    found: list[str] = []
    where: dict[str, str] = {}
    for path in CONTACT_PATHS:
        url = urljoin(website.rstrip("/") + "/", path.lstrip("/"))
        if not allowed_by_robots(url):
            continue
        try:
            resp = session.get(url, timeout=15)
        except requests.RequestException:
            if path == "":
                return "", ""  # homepage unreachable: skip the rest of the site
            continue
        if not resp.ok or "html" not in resp.headers.get("content-type", ""):
            continue
        text = resp.text
        candidates = MAILTO_RE.findall(text) + EMAIL_RE.findall(text)
        for raw in candidates:
            email = _clean(raw)
            if email and email not in found:
                found.append(email)
                where[email] = url
        if any(e.endswith("@" + domain) for e in found):
            break  # good enough, stop fetching more pages
        time.sleep(0.5)
    own = [e for e in found if e.endswith("@" + domain)]
    best = (own or found or [""])[0]
    return best, where.get(best, "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--input", default="leads.csv")
    parser.add_argument("--output", default="leads_with_email.csv")
    parser.add_argument("--all", action="store_true",
                        help="write every lead, not only those with an email")
    parser.add_argument("--workers", type=int, default=6,
                        help="sites checked in parallel (default 6)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    with open(args.input, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    def work(row: dict) -> dict:
        if not row.get("email"):
            row["email"], row["email_source"] = find_email(row["website"], session)
            log.info("%s | %s", row["name"], row["email"] or "-")
        return row

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(work, rows))

    keep = rows if args.all else [r for r in rows if r.get("email")]
    out = Path(args.output)
    with out.open("w", newline="", encoding="utf-8") as f:
        # email_source records the public page the address was published on,
        # which is useful evidence for consent rules such as Canada's CASL.
        fields = CSV_FIELDS + ["email_source"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in keep:
            writer.writerow({k: r.get(k, "") for k in fields})
    log.info("Done: %d of %d leads have an email -> %s",
             sum(1 for r in rows if r.get("email")), len(rows), out.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
