#!/usr/bin/env python3
"""
oid_no_website.py — Extract certified entities from OID that have no website listed,
then optionally search DuckDuckGo to find their online presence.

Phase 1 (default):
    Reads the OID Excel export, pulls all Certified operations with a blank or
    missing Website URL, prioritizes HANDLING scope (most likely to sell products
    online), and writes oid_no_website.csv.

Phase 2 (--search):
    Reads oid_no_website.csv and hits DuckDuckGo HTML for each operation name to
    find a likely website URL.  Adds a `found_url` column and writes
    oid_no_website_searched.csv.  Rate-limited to ~1 req/sec.

Usage:
    # Phase 1 only
    python3 oid_no_website.py

    # Phase 1 + search
    python3 oid_no_website.py --search

    # Custom Excel path
    python3 oid_no_website.py --xlsx /path/to/OID.xlsx

    # Limit search to first N rows (useful for testing)
    python3 oid_no_website.py --search --limit 50

    # Filter to HANDLING scope only (ecommerce/product sellers)
    python3 oid_no_website.py --handling-only
"""

import argparse
import csv
import os
import random
import re
import sys
import time
import urllib.parse

import openpyxl
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_XLSX = "/mnt/c/Users/toit_/OneDrive/OID.OperationSearchResults.2026.4.16.5_34 PM.xlsx"
DEFAULT_OUT         = os.path.join(os.path.dirname(__file__), "oid_no_website.csv")
DEFAULT_SEARCHED    = os.path.join(os.path.dirname(__file__), "oid_no_website_searched.csv")
SEARCH_DELAY        = 0.5   # seconds between rows (slug probes have built-in 0.15s gaps)
SEARCH_JITTER       = 0.5   # random extra delay 0–0.5s

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Column names from OID Excel
# ---------------------------------------------------------------------------

COL_OP_NAME     = "Operation Name"
COL_URL         = "Website URL"
COL_STATUS      = "Operation Certification Status"
COL_CERTIFIER   = "Certifier Name"
COL_CITY        = "City"
COL_STATE       = "State"
COL_CROPS       = "CROPS Scope Certification Status"
COL_LIVESTOCK   = "LIVESTOCK Scope Certification Status"
COL_HANDLING    = "HANDLING Scope Certification Status"
COL_WILD        = "WILD CROPS Scope Certification Status"
COL_PRIVATE_LBL = "Private Labeler"
COL_BROKER      = "Broker"
COL_DISTRIBUTOR = "Distributor"
COL_MARKETER    = "Marketer/Trader"

OUTPUT_COLS = [
    "priority", "operation_name", "city", "state", "certifier",
    "handling_scope", "crops_scope", "livestock_scope", "wild_crops_scope",
    "private_labeler", "broker", "distributor", "marketer_trader",
]

SEARCHED_COLS = OUTPUT_COLS + ["found_url", "search_confidence"]


# ---------------------------------------------------------------------------
# Phase 1 — Extract no-website rows from OID Excel
# ---------------------------------------------------------------------------

def load_no_website(xlsx_path: str, handling_only: bool = False) -> list[dict]:
    print(f"Loading {xlsx_path} …")
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb.active
    all_rows = list(ws.iter_rows(values_only=True))
    wb.close()

    headers   = list(all_rows[0])
    data_rows = all_rows[3:]   # rows 1-2 are OID metadata

    def col(row, name):
        try:
            idx = headers.index(name)
            v   = row[idx]
            return str(v).strip() if v is not None else ""
        except (ValueError, IndexError):
            return ""

    results = []
    skipped_url = skipped_status = 0

    for row in data_rows:
        status = col(row, COL_STATUS)
        if status != "Certified":
            skipped_status += 1
            continue

        url = col(row, COL_URL)
        if url:
            skipped_url += 1
            continue   # has a website already — skip (even if no http:// prefix)

        handling = col(row, COL_HANDLING)
        if handling_only and "Certified" not in handling:
            continue

        # Priority score: HANDLING + product-seller roles score highest
        score = 0
        if "Certified" in handling:                    score += 10
        if "Certified" in col(row, COL_CROPS):        score += 3
        if "Certified" in col(row, COL_LIVESTOCK):    score += 3
        if "yes" in col(row, COL_PRIVATE_LBL).lower(): score += 5
        if "yes" in col(row, COL_BROKER).lower():      score += 4
        if "yes" in col(row, COL_DISTRIBUTOR).lower(): score += 4
        if "yes" in col(row, COL_MARKETER).lower():    score += 4

        results.append({
            "priority":        score,
            "operation_name":  col(row, COL_OP_NAME),
            "city":            col(row, COL_CITY),
            "state":           col(row, COL_STATE),
            "certifier":       col(row, COL_CERTIFIER),
            "handling_scope":  handling,
            "crops_scope":     col(row, COL_CROPS),
            "livestock_scope": col(row, COL_LIVESTOCK),
            "wild_crops_scope":col(row, COL_WILD),
            "private_labeler": col(row, COL_PRIVATE_LBL),
            "broker":          col(row, COL_BROKER),
            "distributor":     col(row, COL_DISTRIBUTOR),
            "marketer_trader": col(row, COL_MARKETER),
        })

    results.sort(key=lambda r: -r["priority"])

    print(f"  Total certified rows        : {len(results) + skipped_url + skipped_status:,}")
    print(f"  Already have website        : {skipped_url:,}")
    print(f"  Not certified (skipped)     : {skipped_status:,}")
    print(f"  No website (our targets)    : {len(results):,}")
    if handling_only:
        print(f"  (filtered to HANDLING scope only)")
    return results


# ---------------------------------------------------------------------------
# Phase 2 — DuckDuckGo search for each operation name
# ---------------------------------------------------------------------------

_STRIP_WORDS = {
    "llc", "inc", "corp", "co", "ltd", "lp", "llp", "dba",
    "the", "and", "of", "at", "an", "a",
    "company", "enterprises", "cooperative", "coop",
}

_TLDS = [".com", ".net", ".org", ".co", ".farm", ".bio"]

DDG_URL = "https://html.duckduckgo.com/html/"

_LEGAL_RE = re.compile(
    r"\b(llc|inc|corp|ltd|lp|llp|co\.?|dba)\b\.?", re.IGNORECASE
)


def _significant_tokens(name: str) -> set[str]:
    """Return lowercase alphabetic tokens ≥3 chars, excluding stop/legal words."""
    return {t for t in re.findall(r"[a-z]{3,}", name.lower()) if t not in _STRIP_WORDS}


def _ddg_query(session: requests.Session, query: str) -> list[str]:
    """
    Search DuckDuckGo HTML interface, return up to 5 organic result URLs.
    DDG wraps real URLs as: /l/?uddg=<encoded_url>&...
    """
    try:
        r = session.get(DDG_URL, params={"q": query, "kl": "us-en"}, timeout=12)
        r.raise_for_status()
    except Exception:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    urls = []
    for a in soup.select("a.result__a"):
        href = a.get("href", "")
        # Absolute DDG redirect: https://duckduckgo.com/l/?uddg=...
        if "uddg=" in href:
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                real = qs.get("uddg", [""])[0]
                if real.startswith("http") and "duckduckgo.com" not in real:
                    urls.append(real)
            except Exception:
                pass
        elif href.startswith("http") and "duckduckgo.com" not in href:
            urls.append(href)
        if len(urls) >= 5:
            break

    return urls


def _name_match_confidence(url: str, name_tokens: set[str]) -> str:
    """
    Score how well a URL's domain matches the operation name.
    Returns 'high' (2+ tokens), 'medium' (1 token), or 'none'.
    Uses substring matching so compound domains (e.g. 'wishfarms') match
    individual name tokens ('wish', 'farms').
    """
    try:
        netloc = urllib.parse.urlparse(url).netloc
    except Exception:
        return "none"
    slug = re.sub(r"^www\.", "", netloc)
    slug = re.sub(r"\.[a-z]{2,10}$", "", slug)          # strip TLD
    slug_lower = slug.lower()

    # Exact token match first (handles hyphenated domains like wish-farms)
    slug_tokens = set(re.findall(r"[a-z]{3,}", slug_lower))
    overlap = slug_tokens & name_tokens
    if len(overlap) >= 2:
        return "high"
    if len(overlap) == 1:
        return "medium"

    # Substring match handles compound domains (wishfarms, avanitea, etc.)
    sub_matches = sum(1 for t in name_tokens if t in slug_lower)
    if sub_matches >= 2:
        return "high"
    if sub_matches == 1:
        return "medium"

    return "none"


def probe_url(url: str, session: requests.Session, timeout: int = 6) -> bool:
    """Return True if the URL responds with a non-error HTTP status."""
    try:
        r = session.head(url, timeout=timeout, allow_redirects=True)
        return r.status_code < 400
    except Exception:
        try:
            r = session.get(url, timeout=timeout, allow_redirects=True)
            return r.status_code < 400
        except Exception:
            return False


def _candidate_slugs(name: str) -> list[str]:
    """Generate likely domain slugs from an operation name (fallback only)."""
    dba_match = re.search(r"\bdba\b\s+(.+)", name, re.IGNORECASE)
    if dba_match:
        name = dba_match.group(1)
    name = re.sub(r"\s*[-–]\s*.+$", "", name).strip()
    tokens = [t for t in re.findall(r"[a-z]+", name.lower()) if t not in _STRIP_WORDS]
    if not tokens:
        return []
    slugs = ["".join(tokens)]
    if len(tokens) >= 2:
        slugs.append("".join(tokens[:2]))
        slugs.append("".join(tokens[:3]))
    if len(tokens) >= 4:
        slugs.append("".join(t[0] for t in tokens))
    return list(dict.fromkeys(slugs))


def search_for_url(session: requests.Session, name: str) -> tuple[str, str]:
    """
    Find the website for an organic operation.
    Returns (found_url, confidence): 'high'|'medium'|'low'|'none'.

    Strategy:
      1. Slug probe — generate domain candidates from the operation name and
         directly test them with HTTP HEAD. Fast and accurate for operations
         whose domain matches their name.
      2. DDG fallback — for operations where slug guessing fails entirely,
         run a DuckDuckGo text query and validate the top results against the
         operation name tokens.
    """
    name_tokens = _significant_tokens(name)
    if not name_tokens:
        return "", "none"

    # ── 1. Slug guessing + HTTP probe ─────────────────────────────────────────
    for slug in _candidate_slugs(name):
        for tld in _TLDS:
            url = f"https://www.{slug}{tld}"
            if probe_url(url, session):
                confidence = _name_match_confidence(url, name_tokens)
                # Accept any non-'none' match; at minimum return 'low'
                return url, confidence if confidence != "none" else "low"
            time.sleep(0.15)

    # ── 2. DDG fallback (for names where slug guessing finds nothing) ─────────
    clean_name = _LEGAL_RE.sub("", name).strip().rstrip(",")
    for query in [f'"{clean_name}" organic', f'{clean_name} organic farm']:
        result_urls = _ddg_query(session, query)
        for url in result_urls:
            confidence = _name_match_confidence(url, name_tokens)
            if confidence in ("high", "medium"):
                return url, confidence
        if result_urls:
            # DDG found something but name didn't match — keep as low-confidence
            return result_urls[0], "low"
        time.sleep(1.0)

    return "", "none"


def run_search(in_path: str, out_path: str, limit: int | None, resume: bool = False):
    print(f"\nPhase 2 — searching for websites …")
    print(f"Input : {in_path}")
    print(f"Output: {out_path}")

    with open(in_path, newline="", encoding="utf-8") as f:
        all_rows = list(csv.DictReader(f))

    # ── Resume: skip rows already in the output file ──────────────────────────
    already_done: set[str] = set()
    if resume and os.path.exists(out_path):
        with open(out_path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                already_done.add(r["operation_name"])
        print(f"  Resuming: {len(already_done):,} rows already done, "
              f"{len(all_rows) - len(already_done):,} remaining.")

    rows = [r for r in all_rows if r["operation_name"] not in already_done]

    if limit:
        rows = rows[:limit]
        print(f"Limit : first {limit} remaining rows")

    total_remaining = len(rows)
    total_all       = len(all_rows)
    file_mode       = "a" if already_done else "w"

    session = requests.Session()
    session.headers.update({"User-Agent": _UA})

    with open(out_path, file_mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SEARCHED_COLS, extrasaction="ignore")
        if file_mode == "w":
            writer.writeheader()

        for i, row in enumerate(rows, 1):
            name = row["operation_name"]
            overall_i = len(already_done) + i
            print(f"  [{overall_i:>6}/{total_all}] {name[:55]:<55}", end=" ", flush=True)

            url, confidence = search_for_url(session, name)
            row["found_url"]         = url
            row["search_confidence"] = confidence

            icon = {"high": "✓✓", "medium": "✓", "low": "~", "none": "—", "error": "✗"}.get(confidence, "?")
            print(f"{icon}  {url[:60]}", flush=True)

            writer.writerow(row)
            f.flush()

            if i < total_remaining:
                time.sleep(SEARCH_DELAY + random.random() * SEARCH_JITTER)

    # Tally across the full output file
    with open(out_path, newline="", encoding="utf-8") as f:
        all_results = list(csv.DictReader(f))
    found = sum(1 for r in all_results if r.get("found_url"))
    print(f"\nDone. {found}/{len(all_results)} operations found a candidate URL.")
    print(f"Results: {out_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Extract OID certified entities with no website, then optionally find them online."
    )
    parser.add_argument("--xlsx", default=DEFAULT_XLSX,
                        help="Path to OID Excel export")
    parser.add_argument("--out", default=DEFAULT_OUT,
                        help="Phase 1 output CSV (default: oid_no_website.csv)")
    parser.add_argument("--search", action="store_true",
                        help="Run Phase 2: DuckDuckGo search for each operation")
    parser.add_argument("--searched-out", default=DEFAULT_SEARCHED,
                        help="Phase 2 output CSV (default: oid_no_website_searched.csv)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit Phase 2 to first N rows")
    parser.add_argument("--handling-only", action="store_true",
                        help="Phase 1: only include HANDLING scope certified operations")
    parser.add_argument("--resume", action="store_true",
                        help="Phase 2: skip rows already in the output CSV and append new results")
    args = parser.parse_args()

    # ── Phase 1 ──────────────────────────────────────────────────────────────
    rows = load_no_website(args.xlsx, handling_only=args.handling_only)

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nPhase 1 complete. Written to: {args.out}")

    # Priority breakdown
    high   = sum(1 for r in rows if r["priority"] >= 10)
    medium = sum(1 for r in rows if 4 <= r["priority"] < 10)
    low    = sum(1 for r in rows if r["priority"] < 4)
    print(f"  High priority  (score ≥ 10, HANDLING): {high:,}")
    print(f"  Medium priority (score 4-9)           : {medium:,}")
    print(f"  Low priority   (score < 4, crops only): {low:,}")
    print()
    print(f"Next steps:")
    print(f"  • Review {args.out} — filter to high-priority rows in Excel")
    print(f"  • Run with --search to find candidate websites via DuckDuckGo")
    print(f"  • Run with --handling-only to narrow to product sellers only")

    # ── Phase 2 (optional) ───────────────────────────────────────────────────
    if args.search:
        run_search(args.out, args.searched_out, args.limit, resume=args.resume)


if __name__ == "__main__":
    main()
