#!/usr/bin/env python3
"""
prescan.py — Platform pre-scan for OID Excel export.

Reads the USDA OID Excel file, extracts all certified operations that have
a website URL, detects their web platform via lightweight HTTP probing, and
writes prescan_results.csv.  Run this BEFORE batch_runner.py.

Usage:
    python3 prescan.py [path/to/OID.xlsx] [--workers N] [--out results.csv]

Defaults:
    xlsx   : OID.OperationSearchResults.2026.4.16.5_34 PM.xlsx (in parent dir)
    workers: 20  (concurrent HTTP requests — increase if you have fast internet)
    out    : prescan_results.csv
"""

import csv
import os
import re
import sys
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import openpyxl

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_XLSX = os.path.join(
    os.path.dirname(__file__),
    "..",  # project root or wherever you keep it
    "OID.OperationSearchResults.2026.4.16.5_34 PM.xlsx",
)
DEFAULT_OUT     = os.path.join(os.path.dirname(__file__), "prescan_results.csv")
DEFAULT_WORKERS = 20
REQUEST_TIMEOUT = 10   # seconds per HTTP request

# Columns we care about in the Excel file
COL_OP_NAME     = "Operation Name"
COL_URL         = "Website URL"
COL_STATUS      = "Operation Certification Status"
COL_CERTIFIER   = "Certifier Name"
COL_CROPS       = "CROPS Scope Certification Status"
COL_LIVESTOCK   = "LIVESTOCK Scope Certification Status"
COL_HANDLING    = "HANDLING Scope Certification Status"
COL_WILD        = "WILD CROPS Scope Certification Status"
COL_PRIVATE_LBL = "Private Labeler"
COL_BROKER      = "Broker"
COL_DISTRIBUTOR = "Distributor"
COL_MARKETER    = "Marketer/Trader"

_UA = "Mozilla/5.0 (compatible; OWC-Prescan/1.0 +https://www.organicwebchecker.com)"


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def detect_platform(url: str, timeout: int = REQUEST_TIMEOUT) -> str:
    """
    Lightweight platform detection via HTTP.
    Priority: Shopify (API probe) > WooCommerce (API probe) >
              HTML signature matches > Custom/WordPress fallback.
    """
    s = requests.Session()
    s.headers.update({"User-Agent": _UA})
    base = url.rstrip("/")

    # ── 1. Shopify: probe /products.json (most reliable, no HTML needed) ──
    try:
        r = s.get(f"{base}/products.json?limit=1", timeout=timeout,
                  allow_redirects=True)
        if r.status_code == 200:
            try:
                if "products" in r.json():
                    return "Shopify"
            except Exception:
                pass
    except Exception:
        pass

    # ── 2. WooCommerce: probe WP REST ─────────────────────────────────────
    try:
        r = s.get(f"{base}/wp-json/wc/v3/products?per_page=1", timeout=timeout,
                  allow_redirects=True)
        if r.status_code in (200, 401, 403):   # 401/403 means endpoint exists
            return "WooCommerce"
    except Exception:
        pass

    # ── 3. HTML signature scan ────────────────────────────────────────────
    try:
        r = s.get(url, timeout=timeout, allow_redirects=True)
        html = r.text.lower()

        sigs = [
            ("Shopify",      ["cdn.shopify.com", "shopify.theme", "shopify.com/s/files"]),
            ("Squarespace",  ["static1.squarespace.com", "squarespace.com/", "sqs-layout"]),
            ("BigCommerce",  ["cdn11.bigcommerce.com", "bigcommerce.com/", "bc-sf-filter"]),
            ("Wix",          ["wixstatic.com", "wix.com/dpages", "parastorage.com"]),
            ("Webflow",      ["webflow.io", "js.webflow.com", "assets.website-files.com"]),
            ("Weebly",       ["weebly.com/", "editmysite.com"]),
            ("GoDaddy",      ["secureservercdn.net", "godaddy.com"]),
            ("WooCommerce",  ["woocommerce", "/wc-ajax="]),
            ("WordPress",    ["wp-content/themes", "wp-includes/js"]),
            ("Magento",      ["mage-", 'require(["magento']),
        ]
        for platform, markers in sigs:
            if any(m in html for m in markers):
                return platform

        return "Custom"

    except requests.exceptions.SSLError:
        return "Error/SSL"
    except requests.exceptions.ConnectionError:
        return "Error/Offline"
    except requests.exceptions.Timeout:
        return "Error/Timeout"
    except Exception:
        return "Error/Other"


# ---------------------------------------------------------------------------
# Excel parsing
# ---------------------------------------------------------------------------

def load_operations(xlsx_path: str) -> list[dict]:
    """
    Load certified operations with website URLs from the OID Excel export.
    The file has 1 header row + 2 metadata rows; data starts at row 4.
    """
    print(f"Loading {xlsx_path} …")
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb.active
    all_rows = list(ws.iter_rows(values_only=True))
    wb.close()

    headers   = list(all_rows[0])   # row 1: column names
    data_rows = all_rows[3:]        # rows 1-2 are OID metadata, data starts row 4

    def col(row, name):
        try:
            idx = headers.index(name)
            v   = row[idx]
            return str(v).strip() if v is not None else ""
        except (ValueError, IndexError):
            return ""

    ops = []
    for row in data_rows:
        status = col(row, COL_STATUS)
        if "Certified" not in status:
            continue
        url = col(row, COL_URL)
        if not url or not url.startswith("http"):
            continue
        ops.append({
            "operation_name": col(row, COL_OP_NAME),
            "website_url":    url,
            "certifier":      col(row, COL_CERTIFIER),
            "crops_scope":    col(row, COL_CROPS),
            "livestock_scope": col(row, COL_LIVESTOCK),
            "handling_scope": col(row, COL_HANDLING),
            "wild_crops_scope": col(row, COL_WILD),
            "private_labeler": col(row, COL_PRIVATE_LBL),
            "broker":          col(row, COL_BROKER),
            "distributor":     col(row, COL_DISTRIBUTOR),
            "marketer_trader": col(row, COL_MARKETER),
            "platform":       "",   # filled by scan
        })

    print(f"  {len(ops)} certified operations with website URLs found.")
    return ops


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="OID pre-scan: detect web platforms")
    parser.add_argument("xlsx", nargs="?", default=DEFAULT_XLSX)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--out",     default=DEFAULT_OUT)
    args = parser.parse_args()

    ops = load_operations(args.xlsx)
    total = len(ops)

    print(f"\nScanning {total} URLs with {args.workers} concurrent workers …")
    print("(This usually takes 5-15 minutes depending on internet speed)\n")

    done    = 0
    t_start = time.time()

    def scan_one(op):
        op["platform"] = detect_platform(op["website_url"])
        return op

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(scan_one, op): op for op in ops}
        for fut in as_completed(futures):
            done += 1
            op = fut.result()
            results.append(op)
            elapsed = time.time() - t_start
            rate    = done / elapsed if elapsed > 0 else 0
            eta     = int((total - done) / rate) if rate > 0 else 0
            print(f"\r  {done}/{total}  {op['platform']:<20}  ETA {eta//60}m{eta%60:02d}s", end="", flush=True)

    print(f"\n\nDone in {int(time.time()-t_start)}s.")

    # Platform summary
    from collections import Counter
    counts = Counter(r["platform"] for r in results)
    print("\nPlatform breakdown:")
    for platform, count in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {platform:<25} {count:>5}")

    # Write CSV
    fieldnames = [
        "operation_name", "website_url", "platform", "certifier",
        "crops_scope", "livestock_scope", "handling_scope", "wild_crops_scope",
        "private_labeler", "broker", "distributor", "marketer_trader",
    ]
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(results, key=lambda r: r["platform"]))

    print(f"\nResults written to: {args.out}")
    print("Next step: run  python3 batch_runner.py prescan_results.csv  to start checks.")


if __name__ == "__main__":
    main()
