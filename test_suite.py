"""
Organic Web Checker — Scraper Regression Test Suite
Run: python test_suite.py

Tests the website scraper (get_organic_products) against a curated set of
real operations across different hosting platforms. Does NOT hit the OID or
run a full check — just validates that the scraper returns sensible output.

Add new cases when you find a site that caused a bug.
"""

import sys
import time
from checker import get_organic_products

# ---------------------------------------------------------------------------
# Test cases
# Each dict:
#   name       — operation name (for display)
#   url        — website URL
#   platform   — hosting platform (Shopify / Squarespace / WooCommerce / etc.)
#   expect_min — minimum # of organic products expected (0 = site has none)
#   expect_max — maximum # of organic products expected (None = no upper bound)
#   notes      — what to watch for
# ---------------------------------------------------------------------------

TEST_CASES = [
    # ── Shopify ────────────────────────────────────────────────────────────
    {
        "name":       "SULU ORGANICS LLC",
        "url":        "https://thesulu.com",
        "platform":   "Shopify",
        "expect_min": 5,
        "expect_max": None,
        "notes":      "Baseline Shopify case — many organic products in catalog",
    },

    # ── Squarespace — brewery/restaurant (no product catalog) ─────────────
    {
        "name":       "The Old Bakery Beer Company",
        "url":        "https://www.oldbakerybeer.com/",
        "platform":   "Squarespace",
        "expect_min": 0,
        "expect_max": 5,
        "notes":      "Squarespace brewery — no ecommerce catalog. Scraper should "
                      "return 0–5 results max; anything longer is body-text junk. "
                      "All results must be ≤150 chars.",
    },

    # ── WooCommerce ────────────────────────────────────────────────────────
    # Add a real WooCommerce organic shop here when you find one.
    # {
    #     "name":       "Example WooCommerce Organic Shop",
    #     "url":        "https://example.com",
    #     "platform":   "WooCommerce",
    #     "expect_min": 3,
    #     "expect_max": None,
    #     "notes":      "WooCommerce product grid",
    # },
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_test(tc: dict) -> dict:
    """Run a single test case. Returns result dict with pass/fail."""
    result = {
        "name":     tc["name"],
        "platform": tc["platform"],
        "url":      tc["url"],
        "passed":   True,
        "errors":   [],
        "products": [],
    }

    try:
        products = get_organic_products(tc["url"])
        result["products"] = products
        count = len(products)

        # Check title length — no result should exceed 150 chars
        long_titles = [p for p in products if len(p["title"]) > 150]
        if long_titles:
            result["passed"] = False
            for p in long_titles:
                result["errors"].append(
                    f"Title too long ({len(p['title'])} chars): {p['title'][:80]}…"
                )

        # Check minimum
        if count < tc["expect_min"]:
            result["passed"] = False
            result["errors"].append(
                f"Expected ≥{tc['expect_min']} products, got {count}"
            )

        # Check maximum
        if tc["expect_max"] is not None and count > tc["expect_max"]:
            result["passed"] = False
            result["errors"].append(
                f"Expected ≤{tc['expect_max']} products, got {count} — "
                f"scraper may be grabbing non-product text"
            )

    except Exception as e:
        result["passed"] = False
        result["errors"].append(f"Exception: {e}")

    return result


def main():
    print("\n" + "=" * 70)
    print("  ORGANIC WEB CHECKER — SCRAPER REGRESSION TESTS")
    print("=" * 70)

    total = len(TEST_CASES)
    passed = 0
    failed = 0

    for i, tc in enumerate(TEST_CASES, 1):
        print(f"\n[{i}/{total}] {tc['name']}  ({tc['platform']})")
        print(f"        {tc['url']}")
        if tc.get("notes"):
            print(f"        Note: {tc['notes']}")

        t0 = time.time()
        result = run_test(tc)
        elapsed = time.time() - t0

        status = "✅ PASS" if result["passed"] else "❌ FAIL"
        count  = len(result["products"])
        print(f"        {status}  —  {count} organic product(s) found  ({elapsed:.1f}s)")

        if result["products"]:
            for p in result["products"][:10]:
                url_hint = f"  → {p['url']}" if p.get("url") else ""
                flag = " ⚠ LONG" if len(p["title"]) > 150 else ""
                print(f"          • {p['title'][:100]}{flag}{url_hint}")
            if count > 10:
                print(f"          … and {count - 10} more")

        if result["errors"]:
            for err in result["errors"]:
                print(f"        ERROR: {err}")

        if result["passed"]:
            passed += 1
        else:
            failed += 1

    print("\n" + "=" * 70)
    print(f"  Results: {passed}/{total} passed  |  {failed} failed")
    print("=" * 70 + "\n")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
