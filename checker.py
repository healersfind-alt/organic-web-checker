"""
Organic Web Checker
Compares products marketed as organic on a client website
against their live USDA OID certificate.
"""

import re
import time
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    """
    Lowercase, remove hyphens/spaces between words so that
    'flax seed', 'flaxseed', and 'flax-seed' all become 'flaxseed'.
    Used for matching only — display uses original text.
    """
    t = text.lower()
    t = re.sub(r'[^a-z0-9]', '', t)   # strip everything except letters/digits
    return t


def extract_ingredient_key(product_title: str) -> str:
    """
    Strip marketing noise from a product title and return
    the core ingredient string for matching.
    """
    t = product_title.lower()
    # Remove brand
    t = re.sub(r'sulu organics®?\s*', '', t)
    # Remove noise words/phrases
    noise = (
        r'\b(usda|pure|organic|natural|cold pressed|unrefined|refined|extra virgin|'
        r'virgin|hexane free|non[\s-]?gmo|premium|therapeutic grade|steam distilled|'
        r'certified|wholesale|travel size|carrier oil|all natural|keto|paleo|'
        r'high smoke point|cooking oil|ala \d+%?|raw|food grade|deep|indian|'
        r'expeller pressed|cold-pressed|uncut|high quality|foraha|supreme|'
        r'100%|first cold press)\b'
    )
    t = re.sub(noise, ' ', t)
    # Remove size/quantity
    t = re.sub(r'\d+[\s]*(?:lbs?|oz|fl\.?\s*oz|gallon|g|kg|ml|set of|%)', ' ', t)
    t = re.sub(r'\d+', ' ', t)
    # Remove punctuation
    t = re.sub(r'[^a-z\s]', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()


CERT_QUALIFIERS = re.compile(
    r'\b(extra virgin|virgin|high oleic|ala \d+%?|roman|sweet|moroccan|mct|'
    r'refined|unrefined|raw|cold pressed|steam distilled|expeller pressed)\b',
    re.IGNORECASE
)

def cert_core(cert_item: str) -> str:
    """
    Strip qualifiers from a cert item to get the core ingredient name.
    'Avocado Oil – Extra Virgin' → 'avocado oil'
    'Flax Seed Oil – ALA 50%' → 'flax seed oil'
    """
    t = cert_item.lower()
    t = re.sub(r'\s*[–-]\s*.*', '', t)       # strip everything after dash/em-dash
    t = CERT_QUALIFIERS.sub(' ', t)
    t = re.sub(r'[^a-z\s]', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()


def is_match(website_title: str, cert_item: str) -> bool:
    """
    Returns True if the website product title matches the cert item.

    Strategy:
    1. Normalize both to remove spaces/hyphens (flaxseed == flax seed).
    2. Check if the cert's core ingredient appears anywhere in the
       normalized website title, or vice versa.
    """
    # Normalize cert to its core ingredient (no qualifiers, no dashes)
    core = normalize(cert_core(cert_item))

    # Normalize the website title — keep all words, just collapse spacing
    web_norm = normalize(website_title)

    # Direct substring match (handles space/hyphen variants)
    if core and (core in web_norm or web_norm in core):
        return True

    # Word-level match: every significant word in cert core must appear
    # somewhere in the website title's normalized form
    core_words = [w for w in cert_core(cert_item).split() if len(w) > 2
                  and w not in ('oil', 'seed', 'butter', 'essential')]
    if core_words and all(normalize(w) in web_norm for w in core_words):
        return True

    return False


# ---------------------------------------------------------------------------
# Website scraping
# ---------------------------------------------------------------------------

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

ORGANIC_RE = re.compile(r'\borganic\b', re.IGNORECASE)


def scrape_shopify(base_url: str) -> list[str]:
    """Pull product titles from Shopify JSON API."""
    url = base_url.rstrip('/') + '/products.json?limit=250'
    products = []
    page = 1
    while True:
        r = requests.get(f"{url}&page={page}", headers=HEADERS, timeout=15)
        if r.status_code != 200:
            break
        batch = r.json().get('products', [])
        if not batch:
            break
        products.extend(p['title'] for p in batch)
        if len(batch) < 250:
            break
        page += 1
    return products


def scrape_generic(url: str) -> list[str]:
    """
    Generic HTML scraper. Looks for product titles using common
    CSS patterns across Shopify, WooCommerce, BigCommerce, and
    custom sites. Falls back to any visible text near the word 'organic'.
    """
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
    except Exception as e:
        return [f"ERROR: {e}"]

    soup = BeautifulSoup(r.text, 'html.parser')

    # Common product title selectors across platforms
    selectors = [
        # Shopify
        '.product-item__title', '.product__title', '.product-title',
        '.product-card__title', '.card__heading',
        # WooCommerce
        '.woocommerce-loop-product__title', '.product_title',
        '.wc-block-grid__product-title',
        # BigCommerce
        '.productGrid .card-title', '[data-product-title]',
        # Generic
        '[class*="product"][class*="title"]',
        '[class*="product"][class*="name"]',
        'h2.title', 'h3.title',
    ]

    found = set()
    for sel in selectors:
        for el in soup.select(sel):
            text = el.get_text(strip=True)
            if text and len(text) > 3:
                found.add(text)

    # Fallback: grab JSON-LD product names
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            import json
            data = json.loads(script.string or '')
            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get('@type') == 'Product':
                    name = item.get('name', '')
                    if name:
                        found.add(name)
        except Exception:
            pass

    # Second fallback: meta product titles in page source
    for meta in soup.find_all('meta', {'property': 'og:title'}):
        content = meta.get('content', '')
        if content and ORGANIC_RE.search(content):
            found.add(content)

    return list(found)


def get_organic_products(website_url: str) -> list[str]:
    """
    Try Shopify API first; fall back to generic scraper.
    Returns only products with 'organic' in the title.
    """
    all_products = scrape_shopify(website_url)

    # If Shopify returned nothing useful, fall back
    if len(all_products) < 3:
        all_products = scrape_generic(website_url)

    return [p for p in all_products if ORGANIC_RE.search(p)]


# ---------------------------------------------------------------------------
# OID scraping via Playwright
# ---------------------------------------------------------------------------

def get_oid_cert(operation_name: str) -> dict:
    """
    Searches the USDA Organic Integrity Database for the operation.
    Returns dict with keys: operation, certifier, status, location, products (list)
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        page.goto(
            "https://organic.ams.usda.gov/integrity/",
            wait_until="networkidle",
            timeout=40000
        )
        time.sleep(4)

        # Type into the operation name field
        op_input = page.locator("#operation")
        op_input.click()
        page.keyboard.type(operation_name, delay=120)
        time.sleep(2)
        page.keyboard.press("Tab")
        time.sleep(6)

        body_text = page.inner_text("body")
        browser.close()

    if operation_name.lower() not in body_text.lower():
        return {"error": f"Operation '{operation_name}' not found in OID"}

    # Parse the result row
    lines = [l.strip() for l in body_text.split('\n') if l.strip()]
    result = {"operation": operation_name, "certifier": "", "status": "", "location": "", "products": []}

    for i, line in enumerate(lines):
        if operation_name.upper() in line.upper():
            # Grab surrounding context
            block = "\n".join(lines[i:i+15])

            # Certifier — stop at tab or "Certified" keyword
            cert_match = re.search(r'\[(\w+)\]\s+(.+?)(?:\t|\s{2,}|Certified|$)', block)
            if cert_match:
                result["certifier"] = cert_match.group(2).strip()

            # Status
            if "Certified" in block:
                result["status"] = "Certified"

            # Location
            loc_match = re.search(r'Certified\s+(\w[\w\s]+?)\s+(\w+)\s+United States', block)
            if loc_match:
                result["location"] = f"{loc_match.group(1).strip()}, {loc_match.group(2).strip()}"

            # Products — everything after "HANDLING:" or "CROPS:"
            prod_match = re.search(r'(?:HANDLING|CROPS):.*?:(.*?)(?:\n\d|\Z)', block, re.DOTALL)
            if prod_match:
                raw_products = prod_match.group(1)
            else:
                # Take from after the location block
                prod_match = re.search(r'United States of America\s+(.*)', block, re.DOTALL)
                raw_products = prod_match.group(1) if prod_match else ""

            # Split and clean product list
            items = []
            for item in re.split(r',\s*', raw_products):
                item = item.strip().strip('.')
                item = re.sub(r'^(HANDLING|CROPS|Butters?|Other):\s*', '', item, flags=re.IGNORECASE)
                item = re.sub(r'#.*', '', item).strip()  # remove notes like #Processing...
                if item and len(item) > 2 and not item.startswith('1 '):
                    items.append(item)
            result["products"] = items
            break

    return result


# ---------------------------------------------------------------------------
# Comparison engine
# ---------------------------------------------------------------------------

def run_check(operation_name: str, website_url: str) -> dict:
    """
    Full check: scrape website + OID, compare, return structured report.
    """
    print(f"[1/3] Pulling OID certificate for '{operation_name}'...")
    cert = get_oid_cert(operation_name)
    if "error" in cert:
        return cert

    print(f"[2/3] Scraping website: {website_url}")
    organic_on_site = get_organic_products(website_url)

    print(f"[3/3] Comparing {len(organic_on_site)} organic products against {len(cert['products'])} certified items...")

    verified = []
    flagged = []

    for product in organic_on_site:
        matched = any(is_match(product, cert_item) for cert_item in cert["products"])
        if matched:
            verified.append(product)
        else:
            flagged.append(product)

    return {
        "operation": cert["operation"],
        "certifier": cert["certifier"],
        "status": cert["status"],
        "location": cert["location"],
        "cert_product_count": len(cert["products"]),
        "cert_products": cert["products"],
        "website_url": website_url,
        "website_organic_count": len(organic_on_site),
        "verified": verified,
        "flagged": flagged,
    }


def print_report(report: dict):
    w = 65
    print("\n" + "=" * w)
    print("  ORGANIC WEB CHECKER — COMPLIANCE REPORT")
    print("=" * w)
    print(f"  Operation : {report['operation']}")
    print(f"  Certifier : {report['certifier']}")
    print(f"  Status    : {report['status']}")
    print(f"  Location  : {report['location']}")
    print(f"  Website   : {report['website_url']}")
    print("=" * w)
    print(f"  OID certified products : {report['cert_product_count']}")
    print(f"  Website organic labels : {report['website_organic_count']}")
    print(f"  Verified (on cert)     : {len(report['verified'])}")
    print(f"  FLAGGED (not on cert)  : {len(report['flagged'])}")
    print("=" * w)

    if report["flagged"]:
        print(f"\n  NON-COMPLIANCE FLAGS ({len(report['flagged'])} items)")
        print(f"  Marketed as organic on website — NOT on OID certificate:")
        print("-" * w)
        for item in sorted(report["flagged"]):
            print(f"  ⚠  {item}")
    else:
        print("\n  No flags — all organic-labeled products match certificate.")

    print("\n" + "=" * w + "\n")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) == 3:
        op_name = sys.argv[1]
        site_url = sys.argv[2]
    else:
        # Default test case
        op_name = "SULU ORGANICS LLC"
        site_url = "https://thesulu.com"

    report = run_check(op_name, site_url)

    if "error" in report:
        print(f"Error: {report['error']}")
    else:
        print_report(report)
