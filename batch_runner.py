#!/usr/bin/env python3
"""
batch_runner.py — Run organic web checks in bulk from a prescan CSV.

Reads prescan_results.csv (output of prescan.py), selects a representative
sample, and runs each operation through checker.py's run_check().

All results are written to:
  • batch_results_<session>.csv   — per-check detail (for analysis)
  • stdout                         — structured log lines with timestamps
                                     (copy a session's log from Railway/terminal
                                      and paste to Claude for analysis)

Usage:
    python3 batch_runner.py [prescan_results.csv] [options]

Key options:
    --sample N      Max checks per platform group (default: 100)
    --platform X    Only run checks for this platform (e.g. Shopify)
    --scope X       Filter by scope: CROPS, LIVESTOCK, HANDLING, WILD_CROPS
    --limit N       Hard cap on total checks (default: unlimited)
    --delay N       Seconds to wait between checks (default: 5)
    --out FILE      Output CSV path (default: batch_results_<session>.csv)
    --resume FILE   Resume from a previous batch_results CSV (skips already-done)

Examples:
    # 100 checks per platform, all scopes
    python3 batch_runner.py prescan_results.csv

    # 50 Shopify-only checks
    python3 batch_runner.py prescan_results.csv --platform Shopify --sample 50

    # First 200 total, any platform
    python3 batch_runner.py prescan_results.csv --limit 200

    # Resume an interrupted run
    python3 batch_runner.py prescan_results.csv --resume batch_results_abc123.csv
"""

import csv
import os
import sys
import uuid
import random
import argparse
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timezone

# Must be run from the organic-checker directory so checker.py is importable
sys.path.insert(0, os.path.dirname(__file__))
from checker import run_check

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SESSION_ID = uuid.uuid4().hex[:8]

def ts() -> str:
    """UTC timestamp string for log lines."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def log(tag: str, msg: str = "", **fields):
    """
    Structured log line.  Format is designed for easy parsing after copy-paste:
      [2026-04-17 02:00:01 UTC] [TAG] key=value key="value with spaces" …
    """
    parts = [f"[{ts()} UTC]", f"[{tag}]"]
    if msg:
        parts.append(msg)
    for k, v in fields.items():
        vstr = f'"{v}"' if " " in str(v) else str(v)
        parts.append(f"{k}={vstr}")
    print(" ".join(parts), flush=True)


# ---------------------------------------------------------------------------
# CSV loading + sampling
# ---------------------------------------------------------------------------

def load_prescan(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def sample_operations(rows: list[dict], sample_per_platform: int,
                      platform_filter: str | None, scope_filter: str | None,
                      hard_limit: int | None) -> list[dict]:
    """
    Select up to sample_per_platform rows per platform.
    Filters out error URLs and applies optional platform/scope filters.
    """
    # Drop non-scannable platforms
    usable = [r for r in rows if not r["platform"].startswith("Error/") and r["website_url"]]

    if platform_filter:
        usable = [r for r in usable if r["platform"].lower() == platform_filter.lower()]

    if scope_filter:
        sf = scope_filter.upper()
        key_map = {
            "CROPS":      "crops_scope",
            "LIVESTOCK":  "livestock_scope",
            "HANDLING":   "handling_scope",
            "WILD_CROPS": "wild_crops_scope",
        }
        col = key_map.get(sf)
        if col:
            usable = [r for r in usable if "Certified" in r.get(col, "")]

    # Group by platform and sample
    by_platform = defaultdict(list)
    for r in usable:
        by_platform[r["platform"]].append(r)

    selected = []
    for platform, group in sorted(by_platform.items()):
        random.shuffle(group)
        selected.extend(group[:sample_per_platform])

    random.shuffle(selected)

    if hard_limit:
        selected = selected[:hard_limit]

    return selected


def load_done_set(resume_path: str | None) -> set[str]:
    """Load set of already-completed (operation_name, website_url) tuples."""
    if not resume_path or not os.path.exists(resume_path):
        return set()
    done = set()
    with open(resume_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            done.add((row["operation_name"], row["website_url"]))
    return done


# ---------------------------------------------------------------------------
# Result serialisation
# ---------------------------------------------------------------------------

RESULT_COLS = [
    "session", "idx", "total", "operation_name", "website_url", "platform",
    "certifier", "scope_detected", "crops_scope", "livestock_scope",
    "handling_scope", "wild_crops_scope", "private_labeler",
    "flags", "caution", "marketing", "compliant", "oid_products",
    "status", "error", "duration_sec",
]

def result_row(session, idx, total, op, report, duration) -> dict:
    """Flatten a checker report dict into a CSV row."""
    if "error" in report:
        return {
            "session": session, "idx": idx, "total": total,
            "operation_name": op["operation_name"],
            "website_url":    op["website_url"],
            "platform":       op["platform"],
            "certifier":      op["certifier"],
            "scope_detected": "",
            "crops_scope":    op.get("crops_scope", ""),
            "livestock_scope": op.get("livestock_scope", ""),
            "handling_scope": op.get("handling_scope", ""),
            "wild_crops_scope": op.get("wild_crops_scope", ""),
            "private_labeler": op.get("private_labeler", ""),
            "flags": 0, "caution": 0, "marketing": 0,
            "compliant": 0, "oid_products": 0,
            "status": "error",
            "error": report["error"],
            "duration_sec": round(duration, 1),
        }

    scopes = report.get("scope", [])
    return {
        "session": session, "idx": idx, "total": total,
        "operation_name": op["operation_name"],
        "website_url":    op["website_url"],
        "platform":       op["platform"],
        "certifier":      op["certifier"],
        "scope_detected": "|".join(scopes) if scopes else "",
        "crops_scope":    op.get("crops_scope", ""),
        "livestock_scope": op.get("livestock_scope", ""),
        "handling_scope": op.get("handling_scope", ""),
        "wild_crops_scope": op.get("wild_crops_scope", ""),
        "private_labeler": op.get("private_labeler", ""),
        "flags":       len(report.get("flagged",    [])),
        "caution":     len(report.get("caution",    [])),
        "marketing":   len(report.get("marketing",  [])),
        "compliant":   len(report.get("compliant",  [])),
        "oid_products": len(report.get("oid_products", [])),
        "status": "done",
        "error": "",
        "duration_sec": round(duration, 1),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Organic Web Checker batch runner")
    parser.add_argument("prescan",  nargs="?",
                        default=os.path.join(os.path.dirname(__file__), "prescan_results.csv"))
    parser.add_argument("--sample",   type=int,   default=100,
                        help="Max checks per platform (default: 100)")
    parser.add_argument("--platform", default=None,
                        help="Filter to one platform, e.g. Shopify")
    parser.add_argument("--scope",    default=None,
                        help="Filter by scope: CROPS LIVESTOCK HANDLING WILD_CROPS")
    parser.add_argument("--limit",    type=int,   default=None,
                        help="Hard cap on total checks")
    parser.add_argument("--delay",    type=int,   default=5,
                        help="Seconds between checks (default: 5)")
    parser.add_argument("--out",      default=None,
                        help="Output CSV path")
    parser.add_argument("--resume",   default=None,
                        help="Resume from existing results CSV")
    args = parser.parse_args()

    out_path = args.out or os.path.join(
        os.path.dirname(__file__), f"batch_results_{SESSION_ID}.csv"
    )

    # ── Load & sample ──────────────────────────────────────────────────────
    all_rows = load_prescan(args.prescan)
    sample   = sample_operations(all_rows, args.sample,
                                 args.platform, args.scope, args.limit)
    done_set = load_done_set(args.resume)
    sample   = [op for op in sample
                if (op["operation_name"], op["website_url"]) not in done_set]
    total    = len(sample)

    if total == 0:
        print("No operations to check (all may already be done, or filters too narrow).")
        return

    # Platform summary
    pcounts = Counter(op["platform"] for op in sample)
    log("BATCH_START",
        session=SESSION_ID, total=total,
        platforms="|".join(f"{p}:{c}" for p, c in sorted(pcounts.items())))

    # ── CSV writer ─────────────────────────────────────────────────────────
    csv_mode = "a" if args.resume and os.path.exists(out_path) else "w"
    csv_file = open(out_path, csv_mode, newline="", encoding="utf-8")
    writer   = csv.DictWriter(csv_file, fieldnames=RESULT_COLS)
    if csv_mode == "w":
        writer.writeheader()

    # ── Run checks ─────────────────────────────────────────────────────────
    completed = errors = 0

    for idx, op in enumerate(sample, 1):
        log("CHECK_START",
            idx=f"{idx}/{total}",
            op=op["operation_name"],
            url=op["website_url"],
            platform=op["platform"])

        import time
        t0 = time.time()
        try:
            report = run_check(op["operation_name"], op["website_url"])
        except Exception as exc:
            report = {"error": str(exc)}
            traceback.print_exc()

        duration = time.time() - t0
        row      = result_row(SESSION_ID, idx, total, op, report, duration)

        if row["status"] == "error":
            errors += 1
            log("CHECK_ERROR",
                idx=f"{idx}/{total}",
                op=op["operation_name"],
                error=row["error"],
                duration=f"{duration:.0f}s")
        else:
            completed += 1
            log("CHECK_DONE",
                idx=f"{idx}/{total}",
                op=op["operation_name"],
                platform=op["platform"],
                scope=row["scope_detected"],
                flags=row["flags"],
                caution=row["caution"],
                compliant=row["compliant"],
                duration=f"{duration:.0f}s")

        writer.writerow(row)
        csv_file.flush()

        if idx < total:
            time.sleep(args.delay)

    csv_file.close()

    log("BATCH_END",
        session=SESSION_ID,
        total=total,
        completed=completed,
        errors=errors,
        out=out_path)

    print(f"\n{'='*60}")
    print(f"Batch complete — session: {SESSION_ID}")
    print(f"  Completed : {completed}")
    print(f"  Errors    : {errors}")
    print(f"  Results   : {out_path}")
    print(f"{'='*60}")
    print(f"\nTo analyse, copy the log lines between:")
    print(f"  [BATCH_START] session={SESSION_ID}")
    print(f"  [BATCH_END]   session={SESSION_ID}")
    print(f"and paste them to Claude.")


if __name__ == "__main__":
    main()
