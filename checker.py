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
# Compliance categorization patterns
# Ref: 7 CFR Part 205 (USDA National Organic Program)
# ---------------------------------------------------------------------------

# Marketing language that uses "organic" contextually rather than as a
# specific product claim.  These trigger an Orange / marketing-review flag,
# not a Red / non-compliance flag.
# Ref: 7 CFR § 205.305–306; NOP Guidance Document 5001
MARKETING_RE = re.compile(
    r'\b(?:'
    r'natural\s+and\s+organic'
    r'|organic\s+and\s+natural'
    r'|made\s+with\s+organic'
    r'|all[\s\-]natural\s+(?:&\s*)?organic'
    r'|our\s+organic(?!\s+\w+\s+\w+\s+\w)'
    r'|shop\s+(?:our\s+)?organic'
    r'|certified\s+organic\s+products?'
    r'|organic\s+(?:products?|produce|collection|line|range|selection|offerings?)'
    r'|(?:natural|clean|sustainable)\s+(?:&|and)\s+organic'
    r'|organic\s+(?:meat|poultry|dairy|eggs?)(?!\s+\w+\s+\w+)'   # generic category names
    r'|organic\s+(?:farm|farming|grower|grown)'
    # Operation/facility-level organic claims (e.g. "certified organic brewery")
    r'|certified\s+organic\s+(?:craft\s+)?(?:brewery|winery|distillery|creamery|bakery|farm|operation|producer|facility|company)'
    r'|(?:craft\s+)?organic\s+(?:brewery|winery|distillery|creamery|bakery)'
    r'|all[\s\-]organic\s+(?:beers?|wines?|spirits?|products?|ingredients?|menu)'
    r'|line\s+of\s+(?:all\s+)?organic\s+(?:beers?|wines?|spirits?)'
    r'|organic\s+craft\s+(?:beer|wine|spirits?|brew)'
    r')\b',
    re.IGNORECASE
)


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


def is_marketing_language(title: str) -> bool:
    """
    Returns True if the product title is marketing/category language
    rather than a specific organic product claim.
    Ref: 7 CFR § 205.305–306; NOP Guidance 5001
    """
    return bool(MARKETING_RE.search(title))


# Words too generic to distinguish products in near-matching
_NEAR_STOP = {
    'oil', 'seed', 'seeds', 'butter', 'extract', 'powder',
    'black', 'white', 'red', 'green', 'blue', 'pure',
    'raw', 'cold', 'pressed', 'refined', 'unrefined',
}

def is_near_match(website_title: str, cert_items: list) -> bool:
    """
    Returns True if the website product is a close-but-not-exact match to a
    cert item — indicating a possible name variation rather than a true gap.

    Caution (yellow) sub-categories this detects:
    - Prefix variants: "Flax Oil" ↔ "Flaxseed Oil" (flax ⊂ flaxseed)
    - Word-level overlap when full normalize() match fails
    Ref: certifier review for name alignment, not an NOP violation
    """
    web_key = extract_ingredient_key(website_title)
    web_words = [w for w in web_key.split()
                 if len(w) >= 4 and w.lower() not in _NEAR_STOP]
    if not web_words:
        return False

    for cert_item in cert_items:
        core = cert_core(cert_item).lower()
        cert_words = [w for w in core.split()
                      if len(w) >= 4 and w not in _NEAR_STOP]
        if not cert_words:
            continue

        pw = web_words[0].lower()
        pc = cert_words[0].lower()

        # Prefix relationship (e.g. "flax" ↔ "flaxseed", "hemp" ↔ "hempseed")
        if pw != pc:
            if (pc.startswith(pw) and len(pw) >= 4) or \
               (pw.startswith(pc) and len(pc) >= 4):
                return True

        # Shared significant word that isn't the primary (e.g. okra / okara share "okr")
        for w1 in web_words:
            for w2 in cert_words:
                if w1 != w2 and len(w1) >= 5 and len(w2) >= 5:
                    if w1[:5] == w2[:5]:   # first 5 chars identical
                        return True

    return False


# ---------------------------------------------------------------------------
# Website scraping
# ---------------------------------------------------------------------------

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

ORGANIC_RE = re.compile(r'\borganic\b', re.IGNORECASE)


def scrape_shopify(base_url: str) -> list[dict]:
    """Pull product titles and URLs from Shopify JSON API."""
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
        products.extend({
            "title": p['title'],
            "url": f"{base_url.rstrip('/')}/products/{p['handle']}"
        } for p in batch)
        if len(batch) < 250:
            break
        page += 1
    return products


def scrape_generic(base_url: str) -> list[dict]:
    """
    Generic HTML scraper. Looks for product titles and URLs using common
    CSS patterns across Shopify, WooCommerce, BigCommerce, and
    custom sites. Falls back to any visible text near the word 'organic'.
    """
    try:
        r = requests.get(base_url, headers=HEADERS, timeout=15)
        r.raise_for_status()
    except Exception as e:
        return [{"title": f"ERROR: {e}", "url": ""}]

    soup = BeautifulSoup(r.text, 'html.parser')

    def make_absolute(href: str) -> str:
        if not href:
            return ""
        if href.startswith('http'):
            return href
        return base_url.rstrip('/') + '/' + href.lstrip('/')

    def find_link(el) -> str:
        anchor = el.find_parent('a') or el.find('a')
        return make_absolute(anchor.get('href', '')) if anchor else ""

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
        # Squarespace Commerce
        '.ProductItem-details-title', '.ProductItem-title',
        '.summary-title', '[data-automation="product-title"]',
        # Generic
        '[class*="product"][class*="title"]',
        '[class*="product"][class*="name"]',
        'h2.title', 'h3.title',
    ]

    found = {}  # title -> url
    for sel in selectors:
        for el in soup.select(sel):
            text = el.get_text(strip=True)
            if text and len(text) > 3 and text not in found:
                found[text] = find_link(el)

    # Fallback: grab JSON-LD product names + urls
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            import json
            data = json.loads(script.string or '')
            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get('@type') == 'Product':
                    name = item.get('name', '')
                    if name and name not in found:
                        found[name] = make_absolute(item.get('url', ''))
        except Exception:
            pass

    return [{"title": t, "url": u} for t, u in found.items()]


def get_organic_products(website_url: str) -> list[dict]:
    """
    Try Shopify API first; fall back to generic scraper.
    Returns only products with 'organic' in the title.
    """
    all_products = scrape_shopify(website_url)

    # If Shopify returned nothing useful, fall back
    if len(all_products) < 3:
        all_products = scrape_generic(website_url)

    return [
        p for p in all_products
        if ORGANIC_RE.search(p['title']) and len(p['title']) <= 150
    ]


# ---------------------------------------------------------------------------
# OID scraping via Playwright
# ---------------------------------------------------------------------------

def _clean_oid_search(name: str) -> str:
    """Strip legal suffixes/punctuation before OID search field.
    'Devenish Nutrition, LLC' → 'Devenish Nutrition'
    'Smith & Sons, Inc.' → 'Smith & Sons'
    """
    cleaned = re.sub(
        r',?\s*(LLC|L\.L\.C\.?|Inc\.?|Incorporated|Corp\.?|Corporation|'
        r'Ltd\.?|Limited|Co\.?|LLP|LP|PLLC|PA|PC|DBA)\s*\.?\s*$',
        '', name, flags=re.IGNORECASE
    ).strip().rstrip(',').strip()
    return cleaned or name


def get_oid_cert(operation_name: str) -> dict:
    """
    Searches the USDA Organic Integrity Database for the operation.
    Returns dict with keys: operation, certifier, status, location, products (list)
    """
    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True)
        page = browser.new_page()

        page.goto(
            "https://organic.ams.usda.gov/integrity/",
            wait_until="load",
            timeout=60000
        )
        time.sleep(8)

        # Type into the operation name field
        op_input = page.locator("#operation")
        op_input.click()
        page.keyboard.type(_clean_oid_search(operation_name), delay=120)
        time.sleep(2)
        page.keyboard.press("Tab")
        time.sleep(6)

        body_text = page.inner_text("body")
        browser.close()

    def _oid_norm(s: str) -> str:
        """Strip all non-alphanumeric chars for OID name comparison.
        Handles comma/period variants: 'Devenish Nutrition, LLC' == 'DEVENISH NUTRITION LLC'.
        """
        return re.sub(r'[^a-z0-9]', '', s.lower())

    op_norm = _oid_norm(operation_name)
    if op_norm not in _oid_norm(body_text):
        return {"error": f"Operation '{operation_name}' not found in OID"}

    # Parse the result row
    lines = [l.strip() for l in body_text.split('\n') if l.strip()]
    result = {"operation": operation_name, "certifier": "", "status": "", "location": "", "products": []}

    for i, line in enumerate(lines):
        if op_norm in _oid_norm(line):
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

def run_check(operation_name: str, website_url: str,
              progress_callback=None) -> dict:
    """
    Full check: scrape website + OID, compare, return structured report.

    Categories (ref: 7 CFR Part 205 — USDA NOP):
      flagged   🔴 Not on OID cert — potential non-compliance (§ 205.307)
      caution   🟡 Near-match / name variation — certifier review advised
      marketing 🟠 Marketing-language use of 'organic' (§ 205.305–306)
      verified  ✅ Confirmed on OID certificate
    """
    def _p(step: int, msg: str):
        if progress_callback:
            progress_callback(step, msg)

    _p(1, f"Connecting to USDA Organic Integrity Database…")
    print(f"[1/4] Pulling OID certificate for '{operation_name}'…")
    cert = get_oid_cert(operation_name)
    if "error" in cert:
        return cert

    _p(2, f"OID certificate loaded — {len(cert['products'])} certified products found")
    print(f"[2/4] Scraping website: {website_url}")
    _p(3, f"Scanning {website_url} for organic product claims…")
    organic_on_site = get_organic_products(website_url)

    _p(4, f"Comparing {len(organic_on_site)} website products against "
          f"{len(cert['products'])} certified items…")
    print(f"[3/4] Comparing {len(organic_on_site)} organic products "
          f"against {len(cert['products'])} certified items…")

    verified  = []   # ✅ Confirmed on OID certificate
    flagged   = []   # 🔴 Not on cert — non-compliance risk (§ 205.307)
    caution   = []   # 🟡 Near-match / name variation — certifier review
    marketing = []   # 🟠 Marketing language using "organic" (§ 205.305-306)

    for product in organic_on_site:
        title = product['title']

        # 1. Marketing / category language — orange, not a product claim
        if is_marketing_language(title):
            marketing.append(product)
            continue

        # 2. Exact match against cert
        if any(is_match(title, ci) for ci in cert["products"]):
            verified.append(product)
            continue

        # 3. Near-match — name variation / possible caution
        if is_near_match(title, cert["products"]):
            caution.append(product)
            continue

        # 4. No match — flag as non-compliance risk
        flagged.append(product)

    print(f"[4/4] Done. Flagged={len(flagged)}  Caution={len(caution)}  "
          f"Marketing={len(marketing)}  Verified={len(verified)}")

    return {
        "operation":          cert["operation"],
        "certifier":          cert["certifier"],
        "status":             cert["status"],
        "location":           cert["location"],
        "cert_product_count": len(cert["products"]),
        "cert_products":      cert["products"],
        "website_url":        website_url,
        "website_organic_count": len(organic_on_site),
        "verified":  verified,
        "flagged":   flagged,
        "caution":   caution,
        "marketing": marketing,
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

    if report.get("flagged"):
        print(f"\n  🔴 NON-COMPLIANCE RISK ({len(report['flagged'])} items)")
        print(f"  Marketed as organic on website — NOT on OID certificate (§ 205.307):")
        print("-" * w)
        for item in sorted(report["flagged"], key=lambda x: x['title']):
            url_hint = f"  → {item['url']}" if item.get('url') else ""
            print(f"  ⚠  {item['title']}{url_hint}")
    else:
        print("\n  ✅ No non-compliance flags.")

    if report.get("caution"):
        print(f"\n  🟡 CAUTION — NAME VARIATIONS ({len(report['caution'])} items)")
        print(f"  Near-match: possible name differences requiring certifier review:")
        print("-" * w)
        for item in sorted(report["caution"], key=lambda x: x['title']):
            print(f"  ~  {item['title']}")

    if report.get("marketing"):
        print(f"\n  🟠 MARKETING LANGUAGE REVIEW ({len(report['marketing'])} items)")
        print(f"  Use of 'organic' in marketing context (§ 205.305–306; NOP Guidance 5001):")
        print("-" * w)
        for item in sorted(report["marketing"], key=lambda x: x['title']):
            print(f"  ○  {item['title']}")

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
