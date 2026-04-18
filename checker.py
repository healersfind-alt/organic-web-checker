"""
Organic Web Checker
Compares products marketed as organic on a client website
against their live USDA OID certificate.
"""

import re
import time
import json
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

# ---------------------------------------------------------------------------
# General cert term detection
# Certifiers often list handler/broker products at category level only
# (e.g. "Eggs", "Wine", "Organic Coffee") — no specific product names.
# When all cert items are generic commodity terms, we cannot confirm or deny
# specific website SKUs; they route to caution rather than red flag.
# ---------------------------------------------------------------------------

_GENERAL_CERT_RE = re.compile(
    r'^(?:organic\s+)?(?:'
    r'eggs?|chicken|beef|pork|lamb|turkey|veal|bison|rabbit|venison|'
    r'dairy|milk|cream|butter|cheese|yogurt|whey|casein|'
    r'vegetables?|fruits?|produce|berries|greens|mushrooms?|'
    r'root\s+vegetables?|leafy\s+greens?|'
    r'grain|grains?|wheat|corn|soybeans?|oats?|rice|barley|rye|sorghum|millet|'
    r'coffee|tea|cacao|cocoa|herbs?|spices?|botanicals?|'
    r'wine|beer|spirits?|cider|mead|kombucha|'
    r'honey|maple\s+syrup|agave|'
    r'nuts?|seeds?|legumes?|beans?|lentils?|peas?|'
    r'oil|oils?|vinegar|juice|juices?|'
    r'poultry|livestock|swine|aquaculture|wool|fiber|'
    r'hay|feed|forage|silage|straw|'
    r'flour|sugar|salt|starch|'
    r'dried\s+\w+|fresh\s+\w+|frozen\s+\w+|'
    r'assorted\s+(?:organic\s+)?products?|various\s+organic|'
    r'handling|processed\s+products?'
    r')s?(?:\s+products?)?(?:\s+and\s+\w+)?$',
    re.IGNORECASE
)


def cert_has_only_general_terms(cert_products: list) -> bool:
    """
    Returns True if ALL cert items are generic commodity/category terms with
    no specific product names.  When True, website-specific SKUs route to
    caution rather than red flag — cert scope cannot confirm or deny them.
    Ref: 7 CFR §205.201 (OSP product list detail held by certifier, not OID)
    """
    if not cert_products:
        return False
    return all(bool(_GENERAL_CERT_RE.match(p.strip())) for p in cert_products)


class ScrapeError(Exception):
    """Raised when all website scraping methods fail; recorded in CSV as error."""


def scrape_shopify(base_url: str) -> list[dict]:
    """Pull product titles and URLs from Shopify JSON API."""
    url = base_url.rstrip('/') + '/products.json?limit=250'
    products = []
    page = 1
    while True:
        try:
            r = requests.get(f"{url}&page={page}", headers=HEADERS, timeout=15)
        except requests.exceptions.ReadTimeout:
            break  # site too slow; fall through to next scraper
        except requests.exceptions.RequestException:
            break
        if r.status_code != 200:
            break
        try:
            batch = r.json().get('products', [])
        except (json.JSONDecodeError, ValueError):
            break  # site returned HTML/redirect, not Shopify JSON
        if not batch:
            break
        for p in batch:
            products.append({
                "title": p['title'],
                "url": f"{base_url.rstrip('/')}/products/{p['handle']}",
                "source": "shopify",
            })
            # Also collect organic image alt-text from Shopify product images
            for img in p.get('images', []):
                alt = (img.get('alt') or '').strip()
                if alt and ORGANIC_RE.search(alt) and len(alt) <= 150:
                    products.append({
                        "title": alt,
                        "url": f"{base_url.rstrip('/')}/products/{p['handle']}",
                        "source": "image_alt",
                    })
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

    # Build base results with source tag
    results = [{"title": t, "url": u, "source": "page"} for t, u in found.items()]

    # ── Phase 1: Image alt-text scan ─────────────────────────────────────────
    # Organic label images often carry alt text with specific product claims.
    # Only collect if alt text contains "organic" and looks like a product name.
    alt_seen = set(found.keys())
    for img in soup.find_all('img', alt=True):
        alt = img.get('alt', '').strip()
        if (alt and 3 < len(alt) <= 150
                and alt not in alt_seen
                and ORGANIC_RE.search(alt)):
            link = img.find_parent('a')
            url  = make_absolute(link.get('href', '')) if link else ''
            results.append({"title": alt, "url": url, "source": "image_alt"})
            alt_seen.add(alt)

    # ── Figcaptions ────────────────────────────────────────────────────────────
    for figcap in soup.find_all('figcaption'):
        text = figcap.get_text(strip=True)
        if (text and 3 < len(text) <= 150
                and text not in alt_seen
                and ORGANIC_RE.search(text)):
            fig  = figcap.find_parent('figure')
            link = (fig or figcap).find('a') if fig else figcap.find('a')
            url  = make_absolute(link.get('href', '')) if link else ''
            results.append({"title": text, "url": url, "source": "figcaption"})
            alt_seen.add(text)

    return results


def scrape_woocommerce(base_url: str) -> list[dict]:
    """
    WooCommerce-specific scraper.
    1. Tries unauthenticated WooCommerce REST API (v3, then v2) — many stores
       leave published products publicly readable.
    2. Falls back to scraping /shop, /store, /products pages with
       WooCommerce-specific CSS selectors.
    """
    base = base_url.rstrip('/')
    products = []

    # ── Step 1: WooCommerce REST API ──────────────────────────────────────────
    for ver in ('v3', 'v2'):
        try:
            page = 1
            while True:
                r = requests.get(
                    f"{base}/wp-json/wc/{ver}/products",
                    params={"per_page": 100, "page": page, "status": "publish"},
                    headers=HEADERS, timeout=12
                )
                if r.status_code != 200:
                    break
                batch = r.json()
                if not isinstance(batch, list) or not batch:
                    break
                for p in batch:
                    if not isinstance(p, dict):
                        continue
                    name = p.get('name', '').strip()
                    link = p.get('permalink', '') or (
                        f"{base}/product/{p['slug']}" if p.get('slug') else base
                    )
                    if name:
                        products.append({"title": name, "url": link, "source": "woocommerce"})
                    # Image alt-text from product images
                    for img in p.get('images', []):
                        alt = (img.get('alt') or '').strip()
                        if alt and ORGANIC_RE.search(alt) and len(alt) <= 150:
                            products.append({"title": alt, "url": link, "source": "image_alt"})
                if len(batch) < 100:
                    break
                page += 1
            if products:
                return products
        except Exception:
            pass

    # ── Step 2: HTML scrape of /shop, /store, /products ──────────────────────
    wc_selectors = [
        'h2.woocommerce-loop-product__title',
        'h3.woocommerce-loop-product__title',
        '.woocommerce-loop-product__title',
        '.wc-block-grid__product-title',
        '.wc-block-grid__product-add-to-cart + .wc-block-grid__product-title',
        'li.product .woocommerce-loop-product__title',
        'ul.products h2', 'ul.products h3',
    ]
    for path in ('/shop', '/store', '/products', ''):
        try:
            r = requests.get(f"{base}{path}", headers=HEADERS, timeout=15)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, 'html.parser')
            # Only continue if this looks like a WooCommerce page
            if not soup.select('.woocommerce, [class*="woocommerce"], .wc-block-grid'):
                continue
            found = {}
            for sel in wc_selectors:
                for el in soup.select(sel):
                    text = el.get_text(strip=True)
                    if text and len(text) > 3 and text not in found:
                        anchor = el.find_parent('a') or el.find('a')
                        href = anchor.get('href', '') if anchor else ''
                        found[text] = (href if href.startswith('http')
                                       else f"{base}/{href.lstrip('/')}" if href
                                       else f"{base}{path}")
            if found:
                products.extend(
                    {"title": t, "url": u, "source": "woocommerce"}
                    for t, u in found.items()
                )
                return products
        except Exception:
            continue

    return products


def get_organic_products(website_url: str) -> list[dict]:
    """
    Scraping pipeline: Shopify → WooCommerce → generic HTML.
    Returns only products with 'organic' in the title.
    Raises ScrapeError if all methods fail, so callers can record the failure
    explicitly rather than treating it as "no organic products found."
    """
    all_products = scrape_shopify(website_url)

    if len(all_products) < 3:
        woo = scrape_woocommerce(website_url)
        if woo:
            all_products = woo

    if len(all_products) < 3:
        generic = scrape_generic(website_url)
        # scrape_generic returns an ERROR sentinel on network/HTTP failure
        if generic and generic[0]['title'].startswith('ERROR:'):
            if not all_products:
                err_detail = generic[0]['title'][len('ERROR: '):]
                raise ScrapeError(f"SCRAPE_FAILED: {err_detail}")
            # else: we have some products from Shopify/WooCommerce; proceed
        else:
            all_products = generic

    return [
        p for p in all_products
        if ORGANIC_RE.search(p['title']) and len(p['title']) <= 150
    ]


# ---------------------------------------------------------------------------
# OID scraping via Playwright
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Business type detection (Scope Validator Phase 2)
# Attempts to identify the sub-type of a HANDLING operation:
# Importer, Broker, Distributor, Processor, etc.
# Source: OID detail page (primary) or keyword inference from cert text.
# ---------------------------------------------------------------------------

_BUSINESS_TYPE_FIELD_RE = re.compile(
    r'(?:Business\s*Type|Activity\s*Type|Operation\s*Type|Facility\s*Type'
    r'|Certification\s*Type)\s*[:\|]\s*([^\n\|]{2,60})',
    re.IGNORECASE
)

# Ordered by specificity — more specific terms first
_BT_KEYWORDS = [
    (re.compile(r'\bimport(?:er|ing|s)?\b', re.I),        'Importer'),
    (re.compile(r'\bexport(?:er|ing|s)?\b', re.I),        'Exporter'),
    (re.compile(r'\bbroker(?:age|ing|s)?\b', re.I),       'Broker'),
    (re.compile(r'\btraders?\b', re.I),                   'Trader'),
    (re.compile(r'\bdistribut(?:or|ion|ing)\b', re.I),    'Distributor'),
    (re.compile(r'\bco[- ]?pack(?:er|ing)?\b', re.I),     'Co-Packer'),
    (re.compile(r'\bmanufactur(?:er|ing)\b', re.I),       'Manufacturer'),
    (re.compile(r'\bprocess(?:or|ing)\b', re.I),          'Processor'),
    (re.compile(r'\bpack(?:er|aging|ing)\b', re.I),       'Packer'),
    (re.compile(r'\bretailers?\b', re.I),                 'Retailer'),
    (re.compile(r'\bwarehouse\b', re.I),                  'Warehouse/Storage'),
]

def _parse_business_type(text: str) -> str:
    """
    Extract HANDLING sub-type from OID detail page text or cert block.
    Returns a label ('Importer', 'Broker', 'Processor', etc.) or '' if unknown.
    """
    # Try explicit labeled field first (e.g. "Business Type: Importer")
    m = _BUSINESS_TYPE_FIELD_RE.search(text)
    if m:
        val = m.group(1).strip().rstrip('.,;')
        if val and len(val) < 60:
            return val.title()
    # Keyword detection
    for pattern, label in _BT_KEYWORDS:
        if pattern.search(text):
            return label
    return ""


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
            wait_until="domcontentloaded",
            timeout=90000
        )
        time.sleep(10)

        # Type into the operation name field
        op_input = page.locator("#operation")
        op_input.click()
        page.keyboard.type(_clean_oid_search(operation_name), delay=120)
        time.sleep(2)
        page.keyboard.press("Tab")
        time.sleep(6)

        body_text = page.inner_text("body")

        # ── Phase 2: Navigate to detail page for Business Type ────────────────
        # Attempt to click the matching result row in the Telerik grid.
        # If this fails for any reason, we fall back to keyword detection
        # on the already-captured body text — the check still completes.
        detail_text = ""
        try:
            cleaned_for_click = _clean_oid_search(operation_name)
            # Telerik grid rows — try common selectors
            for sel in [
                f'tr.k-master-row:has-text("{cleaned_for_click}")',
                f'tr[role="row"]:has-text("{cleaned_for_click}")',
                f'.k-grid-content tr:has-text("{cleaned_for_click}")',
                f'table tr:has-text("{cleaned_for_click}")',
            ]:
                try:
                    row = page.locator(sel).first
                    if row.count() > 0 and row.is_visible(timeout=2000):
                        row.click()
                        time.sleep(5)
                        detail_text = page.inner_text("body")
                        break
                except Exception:
                    continue
        except Exception:
            pass

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
    result = {
        "operation":     operation_name,
        "certifier":     "", "status": "", "location": "",
        "scope":         [],   # e.g. ['HANDLING'] or ['CROPS', 'LIVESTOCK']
        "business_type": "",   # Phase 2: e.g. 'Importer', 'Broker', 'Processor'
        "products":      [],
    }

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

            # Scope types — detect from section headers in product block
            scope_found = re.findall(
                r'\b(HANDLING|CROPS|LIVESTOCK|WILD\s*CROPS)\b',
                block, re.IGNORECASE
            )
            result["scope"] = sorted(set(s.upper().replace(' ', '_') for s in scope_found))

            # Business type (Phase 2) — from detail page or keyword inference
            combined = (detail_text or "") + "\n" + block
            result["business_type"] = _parse_business_type(combined)

            # Products — everything after "HANDLING:" or "CROPS:"
            prod_match = re.search(r'(?:HANDLING|CROPS|LIVESTOCK):.*?:(.*?)(?:\n\d|\Z)', block, re.DOTALL)
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
                item = re.sub(r'^(HANDLING|CROPS|LIVESTOCK|Butters?|Other):\s*', '', item, flags=re.IGNORECASE)
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
              cert: dict = None,
              progress_callback=None) -> dict:
    """
    Full check: scrape website + OID, compare, return structured report.

    Pass cert= to skip the live OID fetch (e.g. when using cached data).

    Categories (ref: 7 CFR Part 205 — USDA NOP):
      flagged   🔴 Not on OID cert — potential non-compliance (§ 205.307)
      caution   🟡 Near-match / name variation — certifier review advised
      marketing 🟠 Marketing-language use of 'organic' (§ 205.305–306)
      verified  ✅ Confirmed on OID certificate
    """
    def _p(step: int, msg: str):
        if progress_callback:
            progress_callback(step, msg)

    if cert is None:
        _p(1, f"Connecting to USDA Organic Integrity Database…")
        print(f"[1/4] Pulling OID certificate for '{operation_name}'…")
        cert = get_oid_cert(operation_name)
    else:
        _p(1, f"OID certificate loaded")
        print(f"[1/4] Using pre-loaded OID certificate for '{operation_name}'…")

    if "error" in cert:
        return cert

    _p(2, f"OID certificate loaded — {len(cert['products'])} certified products found")
    print(f"[2/4] Scraping website: {website_url}")
    _p(3, f"Scanning {website_url} for organic product claims…")
    try:
        organic_on_site = get_organic_products(website_url)
    except ScrapeError as e:
        return {"error": str(e)}

    _p(4, f"Comparing {len(organic_on_site)} website products against "
          f"{len(cert['products'])} certified items…")
    print(f"[3/4] Comparing {len(organic_on_site)} organic products "
          f"against {len(cert['products'])} certified items…")

    verified  = []   # ✅ Confirmed on OID certificate
    flagged   = []   # 🔴 Not on cert — non-compliance risk (§ 205.307)
    caution   = []   # 🟡 Near-match / name variation / general cert — certifier review
    marketing = []   # 🟠 Marketing language using "organic" (§ 205.305-306)

    # When cert lists only general commodity terms (e.g. "Eggs", "Wine") the
    # specific SKUs on the website cannot be confirmed or denied from OID alone —
    # route unmatched items to caution rather than red flag.
    cert_general = cert_has_only_general_terms(cert["products"])

    for product in organic_on_site:
        title = product['title']

        # 1. Exact match against cert — check FIRST so verified items are never
        #    misrouted to marketing even if their title contains marketing phrases.
        #    (Fixes: product removed from cert but still marketing-regex-matched)
        if any(is_match(title, ci) for ci in cert["products"]):
            verified.append(product)
            continue

        # 2. Marketing / category language — only reached when NOT on cert
        if is_marketing_language(title):
            marketing.append(product)
            continue

        # 3. Near-match — name variation / possible caution
        if is_near_match(title, cert["products"]):
            caution.append(product)
            continue

        # 4. No match
        if cert_general:
            # Cert shows general terms only; specific website claim can't be
            # verified from OID — escalate to yellow caution with a note
            caution.append({**product, '_reason': 'general_cert'})
        else:
            flagged.append(product)

    print(f"[4/4] Done. Flagged={len(flagged)}  Caution={len(caution)}  "
          f"Marketing={len(marketing)}  Verified={len(verified)}")

    return {
        "operation":          cert["operation"],
        "certifier":          cert["certifier"],
        "status":             cert["status"],
        "location":           cert["location"],
        "scope":              cert.get("scope", []),
        "business_type":      cert.get("business_type", ""),
        "cert_general":       cert_general,
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
# Certifying agent verification
# ---------------------------------------------------------------------------

def verify_certifying_agent(org_name: str) -> str:
    """
    Verify that org_name is a USDA-accredited certifying agent by searching
    the OID Certifiers directory.

    Returns:
      'verified'  — org found in the USDA certifier directory
      'not_found' — page loaded but org name not present
      'error'     — Playwright/network failure; fall back to manual review
    """
    # Normalise: strip legal suffixes (same helper used for operation search)
    clean = _clean_oid_search(org_name).strip()

    def _norm(s: str) -> str:
        return re.sub(r'[^a-z0-9]', '', s.lower())

    try:
        with sync_playwright() as p:
            browser = p.firefox.launch(headless=True)
            page    = browser.new_page()

            page.goto(
                "https://organic.ams.usda.gov/integrity/Certifiers/CertifiersLocationsSearchPage",
                wait_until="domcontentloaded",
                timeout=90000,
            )
            time.sleep(10)   # allow Blazor/SignalR to initialise

            # Try to find the certifier-name search field and type the org name.
            # The certifiers page uses the same Telerik grid pattern as operations;
            # the input ID is not publicly documented so we try common names.
            filled = False
            for sel in [
                '#certifier', '#certifierName', '#name', '#certName',
                'input[placeholder*="certif" i]', 'input[placeholder*="name" i]',
                'input[type="text"]', 'input[type="search"]',
            ]:
                try:
                    el = page.locator(sel).first
                    if el.count() > 0 and el.is_visible(timeout=3000):
                        el.click()
                        page.keyboard.type(clean, delay=100)
                        time.sleep(1)
                        page.keyboard.press("Tab")
                        time.sleep(6)   # wait for Telerik grid to update
                        filled = True
                        break
                except Exception:
                    continue

            body_text = page.inner_text("body")
            browser.close()

        # ── Match check ───────────────────────────────────────────────────────
        # Exact normalised match
        if _norm(clean) in _norm(body_text):
            return 'verified'

        # Partial match: all significant words (>3 chars) from the org name
        # must appear in the page body (handles minor punctuation differences)
        words = [w for w in clean.lower().split() if len(w) > 3]
        if len(words) >= 2 and all(w in body_text.lower() for w in words):
            return 'verified'

        return 'not_found' if filled else 'error'

    except Exception as exc:
        print(f'[WARN] verify_certifying_agent failed: {exc}')
        return 'error'


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
