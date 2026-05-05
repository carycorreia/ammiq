#!/usr/bin/env python3
"""
AMMO IQ Scraper v2.4
────────────────────
Scrapes component prices from multiple vendors and writes best_per_unit
prices to the Firestore project ammiq-d63b2 under the `prices` collection.

v2.4 changes:
  • Grafs:        quantity now parsed from product TITLE — fixes "100ct shown as /500"
  • Midsouth:     caliber keyword required in title — stops wrong-caliber bleed
  • Lucky Gunner: each product matched uniquely — no more same price for every brand
  • eBay:         new module — OAuth Client Credentials + Browse API (brass/metals only)

Usage (local):
    FIREBASE_CREDENTIALS=path/to/creds.json  \\
    FIREBASE_PROJECT_ID=ammiq-d63b2          \\
    python scraper.py

In GitHub Actions:
    FIREBASE_CREDENTIALS env var holds the JSON content (not path).
    FIREBASE_PROJECT_ID = ammiq-d63b2
"""

import os
import re
import sys
import json
import time
import base64
import logging
import datetime
import requests
from bs4 import BeautifulSoup, Tag

# Optional: Playwright for JS-heavy sites (Brownells)
try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

import firebase_admin
from firebase_admin import credentials, firestore

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-7s  %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('scraper.log', mode='w', encoding='utf-8'),
    ],
)
log = logging.getLogger('ammiq')

# ─────────────────────────────────────────────────────────────────────────────
# eBay credentials (Production)
# ─────────────────────────────────────────────────────────────────────────────
EBAY_APP_ID   = os.environ.get('EBAY_APP_ID', '')
EBAY_CERT_ID  = os.environ.get('EBAY_CERT_ID', '')
EBAY_TOKEN_URL   = 'https://api.ebay.com/identity/v1/oauth2/token'
EBAY_BROWSE_URL  = 'https://api.ebay.com/buy/browse/v1/item_summary/search'
_ebay_token_cache = {'token': None, 'expires': 0}

# ─────────────────────────────────────────────────────────────────────────────
# HTTP session — shared, with respectful delays
# ─────────────────────────────────────────────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
})
REQUEST_DELAY = 2.5   # seconds between requests to same domain


# ═════════════════════════════════════════════════════════════════════════════
# UTILITY HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def clean_price(text: str) -> float | None:
    """Extract first dollar amount from a string. Returns None on failure."""
    if not text:
        return None
    m = re.search(r'\$?\s*([\d,]+\.?\d*)', text.replace(',', ''))
    if m:
        try:
            return float(m.group(1).replace(',', ''))
        except ValueError:
            pass
    return None


def extract_qty_from_title(title: str) -> int | None:
    """
    Extract the pack/count quantity from a product title string.

    Handles patterns like:
      "Hornady Brass 38 Special Unprimed Bulk Bag of 100"  → 100
      "500 Count Boxer Primed"                              → 500
      "1000/Box"                                            → 1000
      "CCI #500 Small Pistol Primers 1000/Box"              → 1000
      "Federal GM100 Primers (1,000 ct)"                    → 1000
      "9mm Brass 250pk"                                     → 250
    """
    t = title.lower()
    patterns = [
        # "bulk bag of N" / "bag of N" / "box of N" / "pack of N"
        r'(?:bulk\s+)?(?:bag|box|pack|pouch|jug|bottle|canister)\s+of\s+([\d,]+)',
        # "N count" / "N ct" / "N-count"
        r'([\d,]+)\s*[-–]?\s*(?:count|ct)\b',
        # "N/box" / "N/1000" etc.
        r'([\d,]+)\s*/\s*(?:box|bag|pack|case)',
        # "Npk" / "N pk" (pack abbreviation)
        r'([\d,]+)\s*pk\b',
        # "(N)" standalone in parens
        r'\(([\d,]+)\)',
        # "N rounds" / "N pieces" / "N primers"
        r'([\d,]+)\s*(?:rounds?|pieces?|pcs?|primers?|cases?|casings?)\b',
        # "per N" / "per 1000"
        r'per\s+([\d,]+)',
        # "N-pack"
        r'([\d,]+)\s*-\s*pack',
    ]
    for pattern in patterns:
        m = re.search(pattern, t)
        if m:
            raw = m.group(1).replace(',', '')
            val = int(raw)
            # Sanity: reject unrealistic quantities (0, single digits for components, huge)
            if 10 <= val <= 100_000:
                return val
    return None


def fetch_html(url: str, retries: int = 2, delay: float = REQUEST_DELAY) -> BeautifulSoup | None:
    """GET a URL and return parsed BeautifulSoup, or None on failure."""
    for attempt in range(retries + 1):
        try:
            time.sleep(delay if attempt > 0 else 0)
            r = SESSION.get(url, timeout=20)
            r.raise_for_status()
            return BeautifulSoup(r.text, 'html.parser')
        except Exception as e:
            log.warning(f'  fetch attempt {attempt+1} failed for {url}: {e}')
            time.sleep(delay)
    return None


def title_contains_caliber(title: str, caliber_keywords: list[str]) -> bool:
    """Return True if at least one caliber keyword appears in the title."""
    t = title.lower()
    return any(kw.lower() in t for kw in caliber_keywords)


# ═════════════════════════════════════════════════════════════════════════════
# FIREBASE SETUP
# ═════════════════════════════════════════════════════════════════════════════

def init_firebase() -> firestore.Client:
    raw = os.environ.get('FIREBASE_CREDENTIALS', '')
    if not raw:
        log.error('FIREBASE_CREDENTIALS env var not set.')
        sys.exit(1)

    # GitHub Actions passes the JSON content directly; local dev may pass a path
    if raw.strip().startswith('{'):
        cred_dict = json.loads(raw)
        cred = credentials.Certificate(cred_dict)
    else:
        cred = credentials.Certificate(raw)

    project = os.environ.get('FIREBASE_PROJECT_ID', 'ammiq-d63b2')
    firebase_admin.initialize_app(cred, {'projectId': project})
    return firestore.client()


def write_component(db, component_id: str, data: dict) -> None:
    """Upsert a component document in the `prices` collection."""
    ref = db.collection('prices').document(component_id)
    data['updated_at'] = datetime.datetime.utcnow().isoformat() + 'Z'
    ref.set(data, merge=True)
    log.info(f'  ✓ Firestore: {component_id} = ${data.get("best_per_unit", "?"):.4f}/unit')


# ═════════════════════════════════════════════════════════════════════════════
# COMPONENT DEFINITIONS
# ═════════════════════════════════════════════════════════════════════════════

# Each component defines:
#   category       : firestore category tag
#   grafs_query    : search string for grafs.com
#   caliber_kws    : keywords that MUST appear in a matching title (anti-bleed)
#   typical_unit   : what "per_unit" means (e.g. 'case', 'primer', 'lb')
#   midsouth_query : search string for midsouth (None = skip)
#   lg_query       : search string for lucky gunner (None = skip)
#   pv_query       : search string for powder valley (None = skip)
#   ebay_query     : search string for eBay Browse API (None = skip)

BRASS_COMPONENTS = {
    '9mm_brass': {
        'category': 'brass',
        'caliber_kws': ['9mm', '9 mm', 'luger', 'parabellum', '9x19'],
        'grafs_query': '9mm brass unprimed',
        'midsouth_query': '9mm brass unprimed',
        'lg_query': '9mm brass',
        'ebay_query': '9mm brass cases unprimed once fired',
        'typical_unit': 'case',
    },
    '38_special_brass': {
        'category': 'brass',
        'caliber_kws': ['38 special', '38spl', '.38 spl', '38 spl'],
        'grafs_query': '38 special brass unprimed',
        'midsouth_query': '38 special brass',
        'lg_query': None,
        'ebay_query': '38 special brass cases unprimed',
        'typical_unit': 'case',
    },
    '357_mag_brass': {
        'category': 'brass',
        'caliber_kws': ['357 mag', '.357 mag', '357mag'],
        'grafs_query': '357 magnum brass unprimed',
        'midsouth_query': '357 magnum brass',
        'lg_query': None,
        'ebay_query': '357 magnum brass cases unprimed',
        'typical_unit': 'case',
    },
    '45_acp_brass': {
        'category': 'brass',
        'caliber_kws': ['45 acp', '.45 acp', '45acp', '45 auto'],
        'grafs_query': '45 acp brass unprimed',
        'midsouth_query': '45 acp brass',
        'lg_query': None,
        'ebay_query': '45 ACP brass cases unprimed',
        'typical_unit': 'case',
    },
    '40_sw_brass': {
        'category': 'brass',
        'caliber_kws': ['40 s&w', '40sw', '.40 s&w', '40 smith'],
        'grafs_query': '40 sw brass unprimed',
        'midsouth_query': '40 s&w brass',
        'lg_query': None,
        'ebay_query': '40 S&W brass cases unprimed',
        'typical_unit': 'case',
    },
    '380_acp_brass': {
        'category': 'brass',
        'caliber_kws': ['380 acp', '.380 acp', '380acp', '380 auto', '9mm kurz'],
        'grafs_query': '380 acp brass unprimed',
        'midsouth_query': '380 acp brass',
        'lg_query': None,
        'ebay_query': '380 ACP brass cases unprimed',
        'typical_unit': 'case',
    },
}

PRIMER_COMPONENTS = {
    'cci_spp': {
        'category': 'primers',
        'caliber_kws': ['cci', 'small pistol', 'spp', '#500', 'no. 500'],
        'grafs_query': 'CCI 500 small pistol primers 1000',
        'midsouth_query': 'CCI small pistol primers',
        'lg_query': None,
        'ebay_query': None,   # primers prohibited on eBay
        'typical_unit': 'primer',
    },
    'cci_lpp': {
        'category': 'primers',
        'caliber_kws': ['cci', 'large pistol', 'lpp', '#300', 'no. 300'],
        'grafs_query': 'CCI 300 large pistol primers 1000',
        'midsouth_query': 'CCI large pistol primers',
        'lg_query': None,
        'ebay_query': None,
        'typical_unit': 'primer',
    },
    'federal_spp': {
        'category': 'primers',
        'caliber_kws': ['federal', 'small pistol', 'gm100', 'no. 100', '#100', 'gm 100'],
        'grafs_query': 'Federal small pistol primers 1000',
        'midsouth_query': 'Federal small pistol primers',
        'lg_query': None,
        'ebay_query': None,
        'typical_unit': 'primer',
    },
    'federal_lpp': {
        'category': 'primers',
        'caliber_kws': ['federal', 'large pistol', 'gm150', 'no. 150', '#150', 'gm 150'],
        'grafs_query': 'Federal large pistol primers 1000',
        'midsouth_query': 'Federal large pistol primers',
        'lg_query': None,
        'ebay_query': None,
        'typical_unit': 'primer',
    },
}

POWDER_COMPONENTS = {
    'accurate_no2': {
        'category': 'powders',
        'caliber_kws': ['accurate', 'no. 2', 'no 2', '#2', 'accurate 2'],
        'grafs_query': 'Accurate No 2 powder 1lb',
        'midsouth_query': 'Accurate No 2 powder',
        'lg_query': None,
        'ebay_query': None,   # powder prohibited on eBay
        'typical_unit': 'lb',
    },
    'n320': {
        'category': 'powders',
        'caliber_kws': ['n320', 'vihtavuori n320', 'n 320'],
        'grafs_query': 'Vihtavuori N320 powder 1lb',
        'midsouth_query': 'Vihtavuori N320',
        'lg_query': None,
        'ebay_query': None,
        'typical_unit': 'lb',
    },
    'bullseye': {
        'category': 'powders',
        'caliber_kws': ['bullseye', 'alliant bullseye'],
        'grafs_query': 'Alliant Bullseye powder 1lb',
        'midsouth_query': 'Alliant Bullseye powder',
        'lg_query': None,
        'ebay_query': None,
        'typical_unit': 'lb',
    },
    'unique': {
        'category': 'powders',
        'caliber_kws': ['unique', 'alliant unique'],
        'grafs_query': 'Alliant Unique powder 1lb',
        'midsouth_query': 'Alliant Unique powder',
        'lg_query': None,
        'ebay_query': None,
        'typical_unit': 'lb',
    },
    'w231': {
        'category': 'powders',
        'caliber_kws': ['w231', 'hp-38', 'winchester 231'],
        'grafs_query': 'Winchester W231 powder 1lb',
        'midsouth_query': 'Winchester 231 powder',
        'lg_query': None,
        'ebay_query': None,
        'typical_unit': 'lb',
    },
    'titegroup': {
        'category': 'powders',
        'caliber_kws': ['titegroup', 'hodgdon titegroup'],
        'grafs_query': 'Hodgdon Titegroup powder 1lb',
        'midsouth_query': 'Titegroup powder',
        'lg_query': None,
        'ebay_query': None,
        'typical_unit': 'lb',
    },
    'true_blue': {
        'category': 'powders',
        'caliber_kws': ['true blue', 'ramshot true blue'],
        'grafs_query': 'Ramshot True Blue powder 1lb',
        'midsouth_query': 'True Blue powder',
        'lg_query': None,
        'ebay_query': None,
        'typical_unit': 'lb',
    },
}

METAL_COMPONENTS = {
    'raw_lead': {
        'category': 'metals',
        'caliber_kws': ['raw lead', 'soft lead', 'lead ingot', 'lead alloy'],
        'grafs_query': None,
        'midsouth_query': None,
        'lg_query': None,
        'ebay_query': 'raw lead ingots casting bullets',
        'rotometals_sku': 'pure-lead-ingots',
        'typical_unit': 'lb',
    },
    'lyman_2': {
        'category': 'metals',
        'caliber_kws': ['lyman 2', 'lyman #2', 'lyman no 2', 'no. 2 alloy'],
        'grafs_query': None,
        'midsouth_query': None,
        'lg_query': None,
        'ebay_query': 'lyman 2 alloy bullet casting lead',
        'rotometals_sku': 'lyman-2-bullet-alloy',
        'typical_unit': 'lb',
    },
    'linotype': {
        'category': 'metals',
        'caliber_kws': ['linotype', 'lino type'],
        'grafs_query': None,
        'midsouth_query': None,
        'lg_query': None,
        'ebay_query': 'linotype lead alloy casting bullets',
        'rotometals_sku': 'linotype-lead-alloy',
        'typical_unit': 'lb',
    },
}

ALL_COMPONENTS = {**BRASS_COMPONENTS, **PRIMER_COMPONENTS, **POWDER_COMPONENTS, **METAL_COMPONENTS}


# ═════════════════════════════════════════════════════════════════════════════
# VENDOR: GRAFS.COM
# ═════════════════════════════════════════════════════════════════════════════

def scrape_grafs(query: str, caliber_kws: list[str]) -> list[dict]:
    """
    Search Grafs for a query and return matching offers.
    KEY FIX v2.4: quantity is extracted from the product TITLE, not hardcoded.
    """
    url = f'https://www.grafs.com/search?query={requests.utils.quote(query)}'
    log.info(f'  Grafs: {url}')
    soup = fetch_html(url)
    if not soup:
        return []

    results = []

    # Grafs search results: product cards
    # Try multiple selectors for robustness
    cards = (
        soup.select('div.product-item-info') or
        soup.select('li.product-item') or
        soup.select('div.product-card') or
        soup.select('article.product-item')
    )

    if not cards:
        # Fallback: look for any element containing a price and a title
        log.warning('    Grafs: no product cards found with standard selectors, trying fallback')
        cards = soup.select('[class*="product"]')

    for card in cards[:8]:   # cap at 8 results per search
        # Title
        title_el = (
            card.select_one('a.product-item-link') or
            card.select_one('.product-name a') or
            card.select_one('[class*="product-name"]') or
            card.select_one('a[href*="/product/"]') or
            card.select_one('h2 a') or
            card.select_one('h3 a')
        )
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        href  = title_el.get('href', '')

        # Caliber guard — skip if title doesn't contain any caliber keyword
        if caliber_kws and not title_contains_caliber(title, caliber_kws):
            log.debug(f'    Grafs: skipping "{title[:60]}" — caliber mismatch')
            continue

        # Price
        price_el = (
            card.select_one('.price') or
            card.select_one('[class*="price"]') or
            card.select_one('span.price')
        )
        price = clean_price(price_el.get_text() if price_el else '')
        if not price or price <= 0:
            continue

        # ── QUANTITY (v2.4 FIX) ─────────────────────────────────────────────
        # First try to parse from title
        qty = extract_qty_from_title(title)

        # If not in title, look for a qty element on the card
        if qty is None:
            for sel in ['[class*="qty"]', '[class*="count"]', '[class*="pack"]']:
                el = card.select_one(sel)
                if el:
                    qty = extract_qty_from_title(el.get_text())
                    if qty:
                        break

        # Last resort: fetch the product page
        if qty is None and href and href.startswith('http'):
            log.info(f'    Grafs: fetching product page for qty → {href[:80]}')
            time.sleep(REQUEST_DELAY)
            prod_soup = fetch_html(href)
            if prod_soup:
                # Try page title first
                qty = extract_qty_from_title(
                    prod_soup.select_one('h1') and prod_soup.select_one('h1').get_text() or title
                )
                # Try description
                if qty is None:
                    desc = prod_soup.select_one('[class*="description"]') or prod_soup.select_one('[class*="detail"]')
                    if desc:
                        qty = extract_qty_from_title(desc.get_text())

        if qty is None:
            log.warning(f'    Grafs: could not determine qty for "{title[:60]}" — skipping')
            continue

        per_unit = price / qty
        results.append({
            'source': 'Grafs',
            'title': title,
            'url': href,
            'price': price,
            'qty': qty,
            'per_unit': per_unit,
        })
        log.info(f'    Grafs ✓  {title[:55]}  ${price:.2f}/{qty} = ${per_unit:.4f}/unit')
        time.sleep(REQUEST_DELAY)

    return results


# ═════════════════════════════════════════════════════════════════════════════
# VENDOR: MIDSOUTH SHOOTERS SUPPLY
# ═════════════════════════════════════════════════════════════════════════════

def scrape_midsouth(query: str, caliber_kws: list[str]) -> list[dict]:
    """
    Search Midsouth for a query.
    v2.4 FIX: caliber_kws must appear in title — prevents wrong-caliber price bleed.
    """
    url = f'https://www.midsouthshooterssupply.com/search#q={requests.utils.quote(query)}&t=product'
    log.info(f'  Midsouth: {url}')
    soup = fetch_html(url)
    if not soup:
        return []

    results = []
    cards = (
        soup.select('div.product-item') or
        soup.select('li.product-item') or
        soup.select('[class*="product-tile"]') or
        soup.select('[class*="product-card"]')
    )

    for card in cards[:8]:
        title_el = (
            card.select_one('a.product-title') or
            card.select_one('[class*="product-name"] a') or
            card.select_one('a[href*="/product/"]') or
            card.select_one('h2 a') or
            card.select_one('h3 a')
        )
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        href  = title_el.get('href', '')
        if href and not href.startswith('http'):
            href = 'https://www.midsouthshooterssupply.com' + href

        # ── CALIBER GUARD (v2.4 FIX) ────────────────────────────────────────
        if caliber_kws and not title_contains_caliber(title, caliber_kws):
            log.debug(f'    Midsouth: skipping "{title[:60]}" — caliber mismatch')
            continue

        price_el = (
            card.select_one('[class*="price"]') or
            card.select_one('.price')
        )
        price = clean_price(price_el.get_text() if price_el else '')
        if not price or price <= 0:
            continue

        qty = extract_qty_from_title(title)
        if qty is None:
            # Try to get qty element (Midsouth sometimes shows "per 100")
            qty_el = card.select_one('[class*="per"]')
            if qty_el:
                qty = extract_qty_from_title(qty_el.get_text())
        if qty is None:
            log.warning(f'    Midsouth: could not determine qty for "{title[:60]}" — skipping')
            continue

        per_unit = price / qty
        results.append({
            'source': 'Midsouth',
            'title': title,
            'url': href,
            'price': price,
            'qty': qty,
            'per_unit': per_unit,
        })
        log.info(f'    Midsouth ✓  {title[:55]}  ${price:.2f}/{qty} = ${per_unit:.4f}/unit')
        time.sleep(REQUEST_DELAY)

    return results


# ═════════════════════════════════════════════════════════════════════════════
# VENDOR: LUCKY GUNNER
# ═════════════════════════════════════════════════════════════════════════════

def scrape_lucky_gunner(query: str, caliber_kws: list[str]) -> list[dict]:
    """
    Search Lucky Gunner for brass.
    v2.4 FIX: each product is matched individually — no more first-result bleed.
    """
    url = f'https://www.luckygunner.com/search?q={requests.utils.quote(query)}'
    log.info(f'  Lucky Gunner: {url}')
    soup = fetch_html(url)
    if not soup:
        return []

    results = []
    cards = (
        soup.select('div.product-details') or
        soup.select('[class*="ammo-listing"]') or
        soup.select('[class*="product-item"]') or
        soup.select('div.listing')
    )

    for card in cards[:10]:
        # Title — each card individually (v2.4 fix: was reading from stale variable)
        title_el = (
            card.select_one('h2') or
            card.select_one('h3') or
            card.select_one('[class*="title"]') or
            card.select_one('[class*="name"]')
        )
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        href_el = card.select_one('a[href]')
        href = href_el['href'] if href_el else ''
        if href and not href.startswith('http'):
            href = 'https://www.luckygunner.com' + href

        if caliber_kws and not title_contains_caliber(title, caliber_kws):
            continue

        # Lucky Gunner shows per-round price AND total — look for the per-unit price
        # They typically display "X¢/rd" or "$X.XX/50"
        price_text = card.get_text()

        # Try to find "N¢ per round" or "$N per round" or "$/round"
        cpr_match = re.search(r'([\d.]+)\s*[¢¢]\s*(?:per|/)\s*(?:round|rd)', price_text, re.I)
        if cpr_match:
            per_unit = float(cpr_match.group(1)) / 100.0
            results.append({
                'source': 'Lucky Gunner',
                'title': title,
                'url': href,
                'price': None,
                'qty': 1,
                'per_unit': per_unit,
            })
            log.info(f'    LG ✓  {title[:55]}  {cpr_match.group(1)}¢/rd = ${per_unit:.4f}/unit')
            continue

        # Fallback: total price ÷ quantity
        price_el = card.select_one('[class*="price"]') or card.select_one('.price')
        price = clean_price(price_el.get_text() if price_el else '')
        qty   = extract_qty_from_title(title) or extract_qty_from_title(price_text)
        if price and qty:
            per_unit = price / qty
            results.append({
                'source': 'Lucky Gunner',
                'title': title,
                'url': href,
                'price': price,
                'qty': qty,
                'per_unit': per_unit,
            })
            log.info(f'    LG ✓  {title[:55]}  ${price:.2f}/{qty} = ${per_unit:.4f}/unit')

    return results


# ═════════════════════════════════════════════════════════════════════════════
# VENDOR: POWDER VALLEY
# ═════════════════════════════════════════════════════════════════════════════

def scrape_powder_valley(query: str, caliber_kws: list[str]) -> list[dict]:
    url = f'https://www.powdervalleyinc.com/search?q={requests.utils.quote(query)}'
    log.info(f'  Powder Valley: {url}')
    soup = fetch_html(url)
    if not soup:
        return []

    results = []
    cards = (
        soup.select('[class*="product-card"]') or
        soup.select('[class*="product-item"]') or
        soup.select('div.product')
    )

    for card in cards[:6]:
        title_el = card.select_one('a') or card.select_one('[class*="name"]')
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        href  = title_el.get('href', '')
        if href and not href.startswith('http'):
            href = 'https://www.powdervalleyinc.com' + href

        if caliber_kws and not title_contains_caliber(title, caliber_kws):
            continue

        price_el = card.select_one('[class*="price"]') or card.select_one('.price')
        price = clean_price(price_el.get_text() if price_el else '')
        qty   = extract_qty_from_title(title)

        if price and qty:
            per_unit = price / qty
            results.append({
                'source': 'Powder Valley',
                'title': title,
                'url': href,
                'price': price,
                'qty': qty,
                'per_unit': per_unit,
            })
            log.info(f'    PV ✓  {title[:55]}  ${price:.2f}/{qty} = ${per_unit:.4f}/unit')
            time.sleep(REQUEST_DELAY)

    return results


# ═════════════════════════════════════════════════════════════════════════════
# VENDOR: ROTOMETALS (metals — lead, alloys)
# ═════════════════════════════════════════════════════════════════════════════

ROTOMETALS_PRODUCTS = {
    'pure-lead-ingots': {
        'url': 'https://www.rotometals.com/pure-soft-lead-ingots/',
        'unit': 'lb',
    },
    'lyman-2-bullet-alloy': {
        'url': 'https://www.rotometals.com/lyman-2-alloy/',
        'unit': 'lb',
    },
    'linotype-lead-alloy': {
        'url': 'https://www.rotometals.com/linotype/',
        'unit': 'lb',
    },
}

def scrape_rotometals(sku: str) -> list[dict]:
    info = ROTOMETALS_PRODUCTS.get(sku)
    if not info:
        return []
    url = info['url']
    log.info(f'  Rotometals: {url}')
    soup = fetch_html(url)
    if not soup:
        return []

    results = []
    # Rotometals product page: price by weight tier
    # Look for price per lb from the product options or price table
    price_els = soup.select('[class*="price"]')
    for el in price_els[:3]:
        text = el.get_text(strip=True)
        price = clean_price(text)
        if price and price > 0:
            # Look for weight in same context
            context = el.parent.get_text() if el.parent else text
            qty_match = re.search(r'(\d+)\s*(?:lb|lbs|pound)', context, re.I)
            qty = int(qty_match.group(1)) if qty_match else 1
            per_unit = price / qty
            results.append({
                'source': 'Rotometals',
                'title': soup.select_one('h1').get_text(strip=True) if soup.select_one('h1') else sku,
                'url': url,
                'price': price,
                'qty': qty,
                'per_unit': per_unit,
            })
            log.info(f'    Rotometals ✓  ${price:.2f}/{qty}lb = ${per_unit:.4f}/lb')
            break  # take first (lowest) price

    return results


# ═════════════════════════════════════════════════════════════════════════════
# VENDOR: BROWNELLS (Playwright — JS-rendered)
# ═════════════════════════════════════════════════════════════════════════════

def scrape_brownells(query: str, caliber_kws: list[str]) -> list[dict]:
    if not HAS_PLAYWRIGHT:
        log.warning('  Brownells: Playwright not installed, skipping.')
        return []

    url = f'https://www.brownells.com/search/index.htm?k={requests.utils.quote(query)}'
    log.info(f'  Brownells (Playwright): {url}')
    results = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_extra_http_headers({'User-Agent': SESSION.headers['User-Agent']})
            page.goto(url, timeout=30000, wait_until='domcontentloaded')
            page.wait_for_timeout(3000)
            content = page.content()
            browser.close()

        soup = BeautifulSoup(content, 'html.parser')
        cards = (
            soup.select('[class*="product-card"]') or
            soup.select('[class*="product-item"]') or
            soup.select('[data-sku]')
        )
        for card in cards[:6]:
            title_el = card.select_one('[class*="name"]') or card.select_one('a')
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            href  = title_el.get('href', '')

            if caliber_kws and not title_contains_caliber(title, caliber_kws):
                continue

            price_el = card.select_one('[class*="price"]')
            price    = clean_price(price_el.get_text() if price_el else '')
            qty      = extract_qty_from_title(title)

            if price and qty:
                per_unit = price / qty
                results.append({
                    'source': 'Brownells',
                    'title': title,
                    'url': 'https://www.brownells.com' + href if href and not href.startswith('http') else href,
                    'price': price,
                    'qty': qty,
                    'per_unit': per_unit,
                })
                log.info(f'    Brownells ✓  {title[:55]}  ${price:.2f}/{qty} = ${per_unit:.4f}/unit')

    except Exception as e:
        log.warning(f'  Brownells: Playwright error — {e}')

    return results


# ═════════════════════════════════════════════════════════════════════════════
# VENDOR: eBay Browse API  (NEW in v2.4)
# ═════════════════════════════════════════════════════════════════════════════

def _get_ebay_token() -> str | None:
    """Get (or refresh) an eBay OAuth Client Credentials token."""
    now = time.time()
    if _ebay_token_cache['token'] and now < _ebay_token_cache['expires'] - 60:
        return _ebay_token_cache['token']

    auth = base64.b64encode(f'{EBAY_APP_ID}:{EBAY_CERT_ID}'.encode()).decode()
    try:
        r = requests.post(
            EBAY_TOKEN_URL,
            headers={
                'Authorization': f'Basic {auth}',
                'Content-Type': 'application/x-www-form-urlencoded',
            },
            data='grant_type=client_credentials&scope=https%3A%2F%2Fapi.ebay.com%2Foauth%2Fapi_scope',
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        token = data.get('access_token')
        expires_in = int(data.get('expires_in', 7200))
        _ebay_token_cache['token'] = token
        _ebay_token_cache['expires'] = now + expires_in
        log.info(f'  eBay: OAuth token obtained (expires in {expires_in}s)')
        return token
    except Exception as e:
        log.error(f'  eBay: token fetch failed — {e}')
        return None


def scrape_ebay(query: str, caliber_kws: list[str], max_price_per_unit: float = 5.0) -> list[dict]:
    """
    Search eBay Browse API for the given query.
    NOTE: Only brass cases and metal alloys are appropriate eBay searches.
          Primers and powder are prohibited on eBay — pass ebay_query=None for those.

    Returns list of offer dicts with per_unit price.
    """
    token = _get_ebay_token()
    if not token:
        return []

    params = {
        'q': query,
        'filter': 'buyingOptions:{FIXED_PRICE},conditions:{NEW|USED_EXCELLENT}',
        'sort': 'price',
        'limit': '20',
        'fieldgroups': 'MATCHING_ITEMS',
    }
    headers = {
        'Authorization': f'Bearer {token}',
        'X-EBAY-C-MARKETPLACE-ID': 'EBAY_US',
        'Content-Type': 'application/json',
    }

    log.info(f'  eBay: "{query}"')
    try:
        r = requests.get(EBAY_BROWSE_URL, params=params, headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning(f'  eBay: API request failed — {e}')
        return []

    items = data.get('itemSummaries', [])
    if not items:
        log.info('  eBay: no results returned')
        return []

    results = []
    for item in items:
        title     = item.get('title', '')
        price_val = float(item.get('price', {}).get('value', 0) or 0)
        item_url  = item.get('itemWebUrl', '')
        condition = item.get('condition', '')

        if not title or price_val <= 0:
            continue

        # Caliber guard
        if caliber_kws and not title_contains_caliber(title, caliber_kws):
            continue

        # Skip auction-only listings (shouldn't happen with filter but double-check)
        buying_options = item.get('buyingOptions', [])
        if 'FIXED_PRICE' not in buying_options and buying_options:
            continue

        # Quantity from title
        qty = extract_qty_from_title(title)
        if qty is None:
            # eBay items often have quantity in the subtitle or additionalImages count
            # Try additional text fields
            for field in ['shortDescription', 'itemGroupType']:
                extra = item.get(field, '')
                if extra:
                    qty = extract_qty_from_title(extra)
                    if qty:
                        break

        if qty is None:
            log.debug(f'    eBay: skipping "{title[:60]}" — qty unknown')
            continue

        per_unit = price_val / qty

        # Sanity filter — reject wildly overpriced listings
        if per_unit > max_price_per_unit:
            log.debug(f'    eBay: skipping "{title[:60]}" — ${per_unit:.4f}/unit exceeds ceiling')
            continue

        results.append({
            'source': 'eBay',
            'title': title,
            'url': item_url,
            'price': price_val,
            'qty': qty,
            'per_unit': per_unit,
            'condition': condition,
        })
        log.info(f'    eBay ✓  {title[:55]}  ${price_val:.2f}/{qty} = ${per_unit:.4f}/unit [{condition}]')

    return results


# ═════════════════════════════════════════════════════════════════════════════
# ORCHESTRATOR — scrape one component across all applicable vendors
# ═════════════════════════════════════════════════════════════════════════════

def scrape_component(comp_id: str, comp: dict) -> dict | None:
    """
    Run all applicable vendor scrapers for one component.
    Returns a result dict ready for Firestore, or None if no data found.
    """
    log.info(f'\n── {comp_id} ({comp["category"]}) ──────────────────────────')
    all_offers: list[dict] = []

    # Grafs
    if comp.get('grafs_query'):
        try:
            all_offers += scrape_grafs(comp['grafs_query'], comp.get('caliber_kws', []))
        except Exception as e:
            log.error(f'  Grafs error: {e}')
        time.sleep(REQUEST_DELAY)

    # Midsouth
    if comp.get('midsouth_query'):
        try:
            all_offers += scrape_midsouth(comp['midsouth_query'], comp.get('caliber_kws', []))
        except Exception as e:
            log.error(f'  Midsouth error: {e}')
        time.sleep(REQUEST_DELAY)

    # Lucky Gunner
    if comp.get('lg_query'):
        try:
            all_offers += scrape_lucky_gunner(comp['lg_query'], comp.get('caliber_kws', []))
        except Exception as e:
            log.error(f'  Lucky Gunner error: {e}')
        time.sleep(REQUEST_DELAY)

    # Powder Valley
    if comp.get('pv_query'):
        try:
            all_offers += scrape_powder_valley(comp['pv_query'], comp.get('caliber_kws', []))
        except Exception as e:
            log.error(f'  Powder Valley error: {e}')
        time.sleep(REQUEST_DELAY)

    # Rotometals (metals only)
    if comp.get('rotometals_sku'):
        try:
            all_offers += scrape_rotometals(comp['rotometals_sku'])
        except Exception as e:
            log.error(f'  Rotometals error: {e}')
        time.sleep(REQUEST_DELAY)

    # eBay
    if comp.get('ebay_query'):
        try:
            all_offers += scrape_ebay(comp['ebay_query'], comp.get('caliber_kws', []))
        except Exception as e:
            log.error(f'  eBay error: {e}')
        time.sleep(1)  # eBay API doesn't need as much delay

    if not all_offers:
        log.warning(f'  No offers found for {comp_id}')
        return None

    # Find best (lowest per_unit)
    best = min(all_offers, key=lambda x: x['per_unit'])

    result = {
        'category':     comp['category'],
        'best_per_unit': best['per_unit'],
        'best_source':   best['source'],
        'best_title':    best['title'],
        'best_url':      best['url'],
        'best_qty':      best.get('qty'),
        'best_price':    best.get('price'),
        'typical_unit':  comp.get('typical_unit', 'unit'),
        'all_offers': [
            {k: v for k, v in o.items() if k != 'condition'}
            for o in sorted(all_offers, key=lambda x: x['per_unit'])
        ],
        'offer_count': len(all_offers),
    }

    log.info(
        f'  ★ BEST: {best["source"]} — {best["title"][:50]} '
        f'@ ${best["per_unit"]:.4f}/{comp.get("typical_unit","unit")}'
    )
    return result


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main():
    log.info('═' * 60)
    log.info('AMMO IQ Scraper v2.4')
    log.info(f'Started: {datetime.datetime.utcnow().isoformat()}Z')
    log.info('═' * 60)

    db = init_firebase()

    success = 0
    errors  = 0

    for comp_id, comp in ALL_COMPONENTS.items():
        try:
            result = scrape_component(comp_id, comp)
            if result:
                write_component(db, comp_id, result)
                success += 1
            else:
                errors += 1
        except Exception as e:
            log.error(f'FATAL error on {comp_id}: {e}', exc_info=True)
            errors += 1

        # Polite pause between components
        time.sleep(REQUEST_DELAY)

    log.info('\n' + '═' * 60)
    log.info(f'Done. {success} components priced, {errors} failed.')
    log.info(f'Finished: {datetime.datetime.utcnow().isoformat()}Z')
    log.info('═' * 60)

    if errors > 0:
        sys.exit(1)  # signals GitHub Actions failure


if __name__ == '__main__':
    main()
