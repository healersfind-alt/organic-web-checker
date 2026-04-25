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
import re
import sys
import time

import openpyxl
import requests

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_XLSX = "/mnt/c/Users/toit_/OneDrive/OID.OperationSearchResults.2026.4.16.5_34 PM.xlsx"
DEFAULT_OUT         = os.path.join(os.path.dirname(__file__), "oid_no_website.csv")
DEFAULT_SEARCHED    = os.path.join(os.path.dirname(__file__), "oid_no_website_searched.csv")
SEARCH_DELAY        = 1.2   # seconds between DuckDuckGo requests

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
        if url and url.startswith("http"):
            skipped_url += 1
            continue   # has a website already — skip

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

# Legal-suffix words to strip when building domain guesses (only true stop words)
_STRIP_WORDS = {
    "llc", "inc", "corp", "co", "ltd", "lp", "llp", "dba",
    "the", "and", "of", "at", "an", "a",
    "company", "enterprises", "cooperative", "coop",
}

_TLDS = [".com", ".net", ".org", ".co", ".farm", ".bio"]


def _candidate_slugs(name: str) -> list[str]:
    """Generate likely domain name slugs from an operation name."""
    # Handle DBA: take the part after dba/d.b.a
    dba_match = re.search(r"\bdba\b\s+(.+)", name, re.IGNORECASE)
    if dba_match:
        name = dba_match.group(1)

    # Strip parenthetical suffixes like " - Division Name"
    name = re.sub(r"\s*[-–]\s*.+$", "", name).strip()

    # Tokenise — lowercase alphabetic only
    tokens = re.findall(r"[a-z]+", name.lower())

    # Remove stop/legal words
    tokens = [t for t in tokens if t not in _STRIP_WORDS]

    if not tokens:
        return []

    slugs = []
    full = "".join(tokens)
    slugs.append(full)                                  # e.g. equatorcoffees
    if len(tokens) >= 2:
        slugs.append("".join(tokens[:2]))               # first two words
        slugs.append("".join(tokens[:3]))               # first three
    if len(tokens) >= 4:
        # Acronym
        slugs.append("".join(t[0] for t in tokens))

    return list(dict.fromkeys(slugs))   # deduplicate, preserve order


def probe_url(url: str, session: requests.Session, timeout: int = 6) -> bool:
    """Return True if the URL responds with a 200-ish HTTP status."""
    try:
        r = session.head(url, timeout=timeout, allow_redirects=True)
        return r.status_code < 400
    except Exception:
        try:
            r = session.get(url, timeout=timeout, allow_redirects=True)
            return r.status_code < 400
        except Exception:
            return False


def search_for_url(session: requests.Session, name: str) -> tuple[str, str]:
    """
    Try common domain patterns derived from the operation name.
    Returns (found_url, confidence) where confidence is 'high'|'low'|'none'.
    'high' = domain slug closely matches the operation name tokens.
    'low'  = domain slug is a subset (2-word only, or short acronym).
    """
    slugs = _candidate_slugs(name)
    if not slugs:
        return "", "none"

    tokens = set(re.findall(r"[a-z]+", name.lower())) - _STRIP_WORDS

    for slug in slugs:
        for tld in _TLDS:
            url = f"https://www.{slug}{tld}"
            if probe_url(url, session):
                # Confidence: how much of the slug overlaps with the name tokens
                slug_tokens = set(re.findall(r"[a-z]{3,}", slug))
                overlap = slug_tokens & tokens
                confidence = "high" if len(overlap) >= 2 else "low"
                return url, confidence
            time.sleep(0.15)    # brief gap between probes

    return "", "none"


def run_search(in_path: str, out_path: str, limit: int | None):
    print(f"\nPhase 2 — searching DuckDuckGo for websites …")
    print(f"Input : {in_path}")
    print(f"Output: {out_path}")
    if limit:
        print(f"Limit : first {limit} rows")

    with open(in_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if limit:
        rows = rows[:limit]

    total = len(rows)
    session = requests.Session()
    session.headers.update({"User-Agent": _UA})

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SEARCHED_COLS, extrasaction="ignore")
        writer.writeheader()

        for i, row in enumerate(rows, 1):
            name = row["operation_name"]
            print(f"  [{i:>5}/{total}] {name[:55]:<55}", end=" ", flush=True)

            url, confidence = search_for_url(session, name)
            row["found_url"]         = url
            row["search_confidence"] = confidence

            icon = {"high": "✓✓", "medium": "✓", "low": "~", "none": "—", "error": "✗"}.get(confidence, "?")
            print(f"{icon}  {url[:60]}", flush=True)

            writer.writerow(row)
            f.flush()

            if i < total:
                time.sleep(SEARCH_DELAY)

    found = sum(1 for r in rows if r.get("found_url"))
    print(f"\nDone. {found}/{total} operations found a candidate URL.")
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
        run_search(args.out, args.searched_out, args.limit)


if __name__ == "__main__":
    main()
