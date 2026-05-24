#!/usr/bin/env python3
"""
Ammo Radar — Daily Price Harvester v3.0
Playwright + eBay Browse API + email alerts + dry-run mode.

v2.7: parse_count_qty, title brand/caliber filter, sanity caps, outlier filter, eBay API
v2.8: Powder Valley → Playwright (403), Grafs URL fallback (404)
v2.9: parse_weight_qty for metals, equipment keyword filter, Grafs Playwright fallback
v3.0: parse_count_qty handles "Case of 1000" / "1000-round case" patterns
      title_require_any + title_reject read from components.yaml (were ignored before)
      qty_unit: count from YAML now drives title qty parsing (not just category check)
      brand filter case-insensitive fix

Usage:
  python scraper.py                       # normal daily run
  python scraper.py --dry-run             # scrape, do NOT write to Firebase
  python scraper.py --component cci_sp    # single component
  python scraper.py --category primers    # single category
  python scraper.py --no-email            # suppress alert emails
  python scraper.py --verbose             # debug logging
"""

import os, re, sys, json, time, logging, datetime, argparse, smtplib, asyncio
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dataclasses import dataclass, asdict
from typing import Optional

import yaml, requests
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, firestore

# ── Logging ───────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scraper.log", mode="a"),
    ],
)
log = logging.getLogger("ammoradar")

# ── Config ────────────────────────────────────────────────────────
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
COMPONENTS_F = os.path.join(SCRIPT_DIR, "components.yaml")
DELAY        = 2.5
TIMEOUT      = 18
TODAY        = datetime.date.today().isoformat()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Keywords that flag a listing as equipment/tools rather than raw material
_EQUIPMENT_KEYWORDS = [
    "furnace", "mould", "mold", "ladle", "pot ", "melter", "casting machine",
    "dipper", "ingot mold", "lead pot", "bullet mould", "bullet mold",
    "lee production", "lyman model", "rcbs cast",
]

# ── Data class ────────────────────────────────────────────────────
@dataclass
class PriceOffer:
    vendor:     str
    price:      float
    qty:        float
    unit:       str
    per_unit:   float
    url:        str
    in_stock:   bool = True
    scraped_at: str  = TODAY

# ── CLI ───────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run",   action="store_true")
    p.add_argument("--component", type=str, default=None)
    p.add_argument("--category",  type=str, default=None)
    p.add_argument("--no-email",  action="store_true")
    p.add_argument("--verbose",   action="store_true")
    return p.parse_args()

# ── Firebase ──────────────────────────────────────────────────────
def init_firebase():
    if firebase_admin._apps:
        return firestore.client()
    cred_json = os.environ.get("FIREBASE_CREDENTIALS")
    if cred_json:
        cred = credentials.Certificate(json.loads(cred_json))
    else:
        cred_file = os.path.join(SCRIPT_DIR, "serviceAccount.json")
        if not os.path.exists(cred_file):
            log.error("No Firebase credentials.")
            sys.exit(1)
        cred = credentials.Certificate(cred_file)
    firebase_admin.initialize_app(cred, {
        "projectId": os.environ.get("FIREBASE_PROJECT_ID", "ammiq-pricing")
    })
    return firestore.client()

# ── Quantity parsers ──────────────────────────────────────────────
def parse_price(text: str) -> Optional[float]:
    text = text.strip().replace(",", "")
    m = re.search(r"\$?([\d]+\.[\d]{1,2})", text)
    return float(m.group(1)) if m else None

def get_qty(component: dict, default: float = 1.0) -> float:
    try:
        return float(component.get("unit", default))
    except (ValueError, TypeError):
        return default

def parse_count_qty(text: str) -> Optional[float]:
    """
    Extract round/count quantity from ammo product titles.
    Handles patterns like:
      "3,330 Rounds"          → 3330
      "50-Round Box"          → 50
      "Case of 1000"          → 1000
      "1000-Round Case"       → 1000
      "Value Pack 350rd"      → 350
      "500ct"                 → 500
    """
    # Normalise comma-thousands: 3,330 → 3330
    cleaned = re.sub(r"(\d),(\d{3})\b", r"\1\2", text)

    patterns = [
        # "Case of 1000" / "box of 50"
        r"(?:case|box|pack|value\s+pack)\s+of\s+(\d+)",
        # "1000-round case" / "350 round value pack" / "50 rounds"
        r"(\d+)\s*[–\-]?\s*(?:round|rd|rnd|cartridge|count|ct|pk|pack)s?\b",
        # "per box of 50" / "50/box"
        r"(\d+)\s*(?:per\s+)?box",
        # "500pc" / "500 pc"
        r"\b(\d{2,5})\s*pc\b",
        # bare large number that looks like a case qty: 500, 1000, 2000 etc
        r"\b(500|1000|1500|2000|3000|3330|5000)\b",
    ]
    for pat in patterns:
        m = re.search(pat, cleaned, re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1))
                if val > 1:
                    return val
            except (ValueError, IndexError):
                pass
    return None

def parse_weight_qty(text: str) -> Optional[float]:
    """Extract weight in pounds from metals listings (e.g. '10 lbs', '25+ pounds', '8oz')."""
    cleaned = re.sub(r"(\d),(\d{3})\b", r"\1\2", text)
    for pat in [r"(\d+(?:\.\d+)?)\s*\+?\s*(?:pound|lb)s?\b", r"\b(\d+(?:\.\d+)?)\s*LB\b"]:
        m = re.search(pat, cleaned, re.IGNORECASE)
        if m:
            val = float(m.group(1))
            if val > 0: return val
    # oz → lbs
    m = re.search(r"(\d+(?:\.\d+)?)\s*oz\b", cleaned, re.IGNORECASE)
    if m:
        val = float(m.group(1)) / 16.0
        if val > 0: return val
    return None

# ── Title filters ─────────────────────────────────────────────────
def _is_equipment(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in _EQUIPMENT_KEYWORDS)

def _title_passes_filters(title: str, component: dict, vendor_name: str) -> bool:
    """
    Check title against:
      - brand (component["brand"])
      - caliber (component["caliber"])
      - title_require_any (list: at least one must appear)
      - title_reject      (list: none may appear)
    Returns True if the listing should be kept.
    """
    title_low = title.lower()

    # Brand check (case-insensitive substring)
    brand = component.get("brand", "").lower()
    if brand and brand not in title_low:
        log.debug(f"  {vendor_name}: skip brand '{brand}' — '{title[:60]}'")
        return False

    # Caliber check
    caliber = component.get("caliber", "").lower()
    if caliber and not _caliber_match(title_low, caliber):
        log.debug(f"  {vendor_name}: skip caliber '{caliber}' — '{title[:60]}'")
        return False

    # title_require_any — at least one phrase must be present
    require_any = component.get("title_require_any", [])
    if require_any:
        if not any(phrase.lower() in title_low for phrase in require_any):
            log.debug(f"  {vendor_name}: skip require_any — '{title[:60]}'")
            return False

    # title_reject — none of these may appear
    for phrase in component.get("title_reject", []):
        if phrase.lower() in title_low:
            log.debug(f"  {vendor_name}: skip reject '{phrase}' — '{title[:60]}'")
            return False

    return True

def _caliber_match(title_low: str, caliber: str) -> bool:
    if not caliber: return True
    cal_norm   = caliber.lower().replace("-", "").replace(" ", "")
    title_norm = title_low.replace("-", "").replace(" ", "")
    variants   = {
        cal_norm,
        cal_norm.replace("lr", "longrifle"),
        cal_norm.replace("acp", "auto"),
        cal_norm.replace("spl", "special"),
        cal_norm.replace("mag", "magnum"),
    }
    return any(v in title_norm for v in variants)

def _extract_title(container) -> str:
    for sel in ["h2", "h3", "h4", ".product-name", ".product-title",
                ".item-name", "[class*='title']", "[class*='name']"]:
        try:
            el = container.select_one(sel)
            if el: return el.get_text(" ", strip=True)
        except Exception:
            pass
    try:
        a = container.find("a")
        if a: return a.get_text(" ", strip=True)
    except Exception:
        pass
    return ""

# ── Sanity caps (per-unit price bounds by category) ───────────────
_SANITY_CAPS = {
    "factory_ammo": (0.01, 10.00),   # per round
    "primers":      (0.01, 0.50),    # per primer
    "brass":        (0.01, 2.00),    # per case
    "projectiles":  (0.01, 2.00),    # per bullet
    "powder":       (10.0, 500.0),   # per lb
    "powders":      (10.0, 500.0),
    "metals":       (0.50, 30.0),    # per lb
    "coatings":     (0.01, 200.0),
}

def sanity_per_unit(per_unit: float, category: str) -> bool:
    lo, hi = _SANITY_CAPS.get(category, (0.0, 1e9))
    return lo <= per_unit <= hi

# ── Shared qty resolver ───────────────────────────────────────────
def _resolve_qty(component: dict, title: str) -> float:
    """
    Determine the correct quantity for a listing.
    Uses qty_unit from YAML + title parsing to get accurate pack size.
      qty_unit: count  → parse round count from title
      qty_unit: weight → parse weight (lbs) from title
      anything else   → fall back to YAML unit value
    """
    qty_unit = component.get("qty_unit", "").lower()
    category = component.get("_category", "")

    if qty_unit == "count" or category == "factory_ammo":
        parsed = parse_count_qty(title)
        return parsed if parsed and parsed > 1 else get_qty(component, 50.0)

    if qty_unit == "weight" or category == "metals":
        parsed = parse_weight_qty(title)
        return parsed if parsed and parsed > 0 else get_qty(component, 1.0)

    return get_qty(component)

def _card_to_offer(component, card, price, vendor_name) -> Optional[tuple]:
    """
    Extract title from a BeautifulSoup card, apply all filters, resolve qty.
    Returns (qty, per_unit) or None to skip.
    """
    title    = _extract_title(card)
    category = component.get("_category", "")

    if not _title_passes_filters(title, component, vendor_name):
        return None

    qty      = _resolve_qty(component, title)
    per_unit = round(price / qty, 6) if qty else price

    if not sanity_per_unit(per_unit, category):
        log.warning(f"  {vendor_name}: sanity fail ${per_unit:.4f}/{category} — '{title[:60]}'")
        return None

    return qty, per_unit

# ── Fetch helpers ─────────────────────────────────────────────────
def fetch_static(url: str) -> Optional[BeautifulSoup]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        time.sleep(DELAY)
        return BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        log.warning(f"  Static fetch failed: {e}")
        return None

async def _fetch_js(url: str, wait_selector: str = None, wait_ms: int = 3500):
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx  = await browser.new_context(user_agent=HEADERS["User-Agent"], locale="en-US")
            page = await ctx.new_page()
            await page.goto(url, timeout=30000)
            if wait_selector:
                try: await page.wait_for_selector(wait_selector, timeout=8000)
                except Exception: pass
            else:
                await page.wait_for_timeout(wait_ms)
            html = await page.content()
            await browser.close()
            await asyncio.sleep(DELAY)
            return BeautifulSoup(html, "html.parser")
    except ImportError:
        log.warning("  Playwright not installed — falling back to static fetch")
        return fetch_static(url)
    except Exception as e:
        log.warning(f"  Playwright fetch failed: {e}")
        return None

def fetch_js(url, wait_selector=None, wait_ms=3500):
    return asyncio.run(_fetch_js(url, wait_selector, wait_ms))

# ── eBay Browse API ───────────────────────────────────────────────
def _ebay_token() -> str:
    import base64
    app_id  = os.environ.get("EBAY_APP_ID",  "").strip()
    cert_id = os.environ.get("EBAY_CERT_ID", "").strip()
    if not app_id or not cert_id:
        log.warning("eBay: EBAY_APP_ID / EBAY_CERT_ID not set — skipping")
        return ""
    creds = base64.b64encode(f"{app_id}:{cert_id}".encode()).decode()
    try:
        r = requests.post(
            "https://api.ebay.com/identity/v1/oauth2/token",
            headers={"Authorization": f"Basic {creds}",
                     "Content-Type":  "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials",
                  "scope": "https://api.ebay.com/oauth/api_scope"},
            timeout=15,
        )
        data  = r.json()
        token = data.get("access_token", "")
        if not token:
            log.warning(f"eBay token failed — HTTP {r.status_code} — {data}")
        return token
    except Exception as e:
        log.warning(f"eBay token error: {e}")
        return ""

def scrape_ebay(component) -> list:
    token = _ebay_token()
    if not token: return []
    category = component.get("_category", "")
    offers   = []
    for term in component.get("search_terms", [])[:1]:
        try:
            r = requests.get(
                "https://api.ebay.com/buy/browse/v1/item_summary/search",
                headers={"Authorization":           f"Bearer {token}",
                         "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
                         "Content-Type":            "application/json"},
                params={"q": term, "limit": "25",
                        "filter": "buyingOptions:{FIXED_PRICE}"},
                timeout=15,
            )
            if r.status_code != 200:
                log.warning(f"  eBay API error {r.status_code}")
                continue
            for item in r.json().get("itemSummaries", []):
                title     = item.get("title", "")
                price_val = float(item.get("price", {}).get("value", 0) or 0)
                item_url  = item.get("itemWebUrl", "")
                if not price_val: continue
                if _is_equipment(title): continue
                if not _title_passes_filters(title, component, "eBay"): continue
                qty      = _resolve_qty(component, title)
                per_unit = round(price_val / qty, 6) if qty else price_val
                if not sanity_per_unit(per_unit, category):
                    log.warning(f"  eBay: sanity fail ${per_unit:.4f}/{category} — '{title[:60]}'")
                    continue
                offers.append(PriceOffer("eBay", price_val, qty,
                    str(component.get("unit", "1")), per_unit, item_url))
        except Exception as e:
            log.warning(f"  eBay error: {e}")
    if offers: log.info(f"  ebay: {len(offers)} offer(s)")
    return offers

# ── Vendor scrapers ───────────────────────────────────────────────

def _scrape_cards(component, soup, cards_sel, price_sel, link_sel,
                  vendor_name, base_url, stock_sel=None):
    """Generic card scraper — shared by most static/Playwright vendors."""
    offers   = []
    category = component.get("_category", "")
    for card in soup.select(cards_sel)[:8]:
        price_el = card.select_one(price_sel)
        link_el  = card.select_one(link_sel)
        if not price_el: continue
        price = parse_price(price_el.get_text())
        if not price: continue
        result = _card_to_offer(component, card, price, vendor_name)
        if result is None: continue
        qty, per_unit = result
        href = link_el["href"] if link_el else base_url
        if href.startswith("/"): href = base_url.rstrip("/") + href
        in_stock = True
        if stock_sel:
            stock_el = card.select_one(stock_sel)
            in_stock = "out" not in (stock_el.get_text().lower() if stock_el else "in")
        offers.append(PriceOffer(vendor_name, price, qty,
            str(component.get("unit", "1")), per_unit, href, in_stock))
    return offers

def scrape_powder_valley(component):
    offers = []
    for term in component.get("search_terms", [])[:2]:
        url  = f"https://www.powdervalleyinc.com/search?q={requests.utils.quote(term)}"
        log.info(f"  Powder Valley (Playwright): {url[:70]}")
        soup = fetch_js(url, wait_selector=".product-item-info, .product-item", wait_ms=4000)
        if not soup: continue
        offers += _scrape_cards(
            component, soup,
            ".product-item-info, .product-item, [data-product-id]",
            ".price, .special-price .price, [data-price-type='finalPrice']",
            "a[href]", "Powder Valley", "https://www.powdervalleyinc.com")
    return offers

def scrape_grafs(component):
    offers = []
    for term in component.get("search_terms", [])[:2]:
        encoded = requests.utils.quote(term)
        soup    = None
        for try_url in [f"https://www.grafs.com/search?q={encoded}",
                        f"https://www.grafs.com/catalogsearch/result/?q={encoded}"]:
            s = fetch_static(try_url)
            if s and s.select(".product-item, .item.product, .product-item-info"):
                soup = s; break
        if not soup:
            pw_url = f"https://www.grafs.com/search?q={encoded}"
            log.info(f"  Grafs (Playwright fallback): {pw_url[:70]}")
            soup = fetch_js(pw_url,
                            wait_selector=".product-item, .product-item-info",
                            wait_ms=4500)
        if not soup: continue
        offers += _scrape_cards(
            component, soup,
            ".product-item, .item.product, .product-item-info",
            ".price, .regular-price, [data-price-type='finalPrice']",
            "a.product-item-link, a[href]", "Grafs", "https://www.grafs.com")
    return offers

def scrape_midsouth(component):
    offers = []
    for term in component.get("search_terms", [])[:2]:
        encoded = requests.utils.quote(term)
        soup    = None
        for try_url in [
            f"https://www.midsouthshooterssupply.com/search?keywords={encoded}",
            f"https://www.midsouthshooterssupply.com/search#{encoded}",
        ]:
            s = fetch_static(try_url)
            if s and s.select(".product-container, .product-item, .ms-product-card"):
                soup = s; break
        if not soup: continue
        offers += _scrape_cards(
            component, soup,
            ".product-container, .product-item, .ms-product-card",
            ".product-price, .price-box .price, .ms-price",
            "a[href]", "Midsouth", "https://www.midsouthshooterssupply.com")
    return offers

def scrape_lucky_gunner(component):
    offers = []
    for term in component.get("search_terms", [])[:1]:
        url  = f"https://www.luckygunner.com/search?q={requests.utils.quote(term)}"
        soup = fetch_static(url)
        if not soup: continue
        offers += _scrape_cards(
            component, soup,
            ".product, .ammo-listing, .lg-product-card",
            ".price, .ammo-price, .lg-price",
            "a[href]", "Lucky Gunner", "https://www.luckygunner.com",
            stock_sel=".in-stock, .out-of-stock, .stock-status")
    return offers

def scrape_ammoseek(component):
    """AmmoSeek returns cost-per-round directly — apply sanity cap only."""
    caliber = component.get("caliber", "")
    if not caliber: return []
    category = component.get("_category", "factory_ammo")
    cal_map  = {"9mm": "9mm-luger", "45acp": "45-auto", "38spl": "38-special",
                "357mag": "357-magnum", "22lr": "22-long-rifle"}
    slug  = cal_map.get(caliber, caliber.replace(" ", "-"))
    grain = component.get("grain", "")
    url   = f"https://ammoseek.com/ammo/{slug}" + (f"?gr={grain}" if grain else "")
    log.info(f"  AmmoSeek (Playwright): {url}")
    soup  = fetch_js(url, wait_selector="tr.offer-row, .listing-item", wait_ms=4500)
    if not soup: return []
    brand  = component.get("brand", "").lower()
    offers = []
    for row in soup.select("tr.offer-row, .listing-item, .ammo-row")[:10]:
        price_el  = row.select_one(".price-per-round, .cpr, td.cpr, [data-cpr]")
        vendor_el = row.select_one(".retailer, .vendor-name, td.vendor, .seller")
        link_el   = row.select_one("a[href]")
        stock_el  = row.select_one(".stock, .availability")
        if not price_el: continue
        cpr = parse_price(price_el.get_text())
        if not cpr: continue
        row_text = row.get_text(" ", strip=True).lower()
        if brand and brand not in row_text: continue
        # title_require_any / title_reject on full row text
        require_any = component.get("title_require_any", [])
        if require_any and not any(p.lower() in row_text for p in require_any): continue
        if any(p.lower() in row_text for p in component.get("title_reject", [])): continue
        if not sanity_per_unit(cpr, category):
            log.warning(f"  AmmoSeek: sanity fail ${cpr:.4f}/{category}")
            continue
        box_qty  = get_qty(component, 50.0)
        in_stock = "out" not in (stock_el.get_text().lower() if stock_el else "in")
        offers.append(PriceOffer(
            vendor_el.get_text().strip() if vendor_el else "AmmoSeek",
            round(cpr * box_qty, 4), box_qty, str(component.get("unit", "50")),
            cpr, link_el["href"] if link_el else url, in_stock))
    return offers

def scrape_target_sports(component):
    offers = []
    for term in component.get("search_terms", [])[:1]:
        url  = f"https://www.targetsportsusa.com/search.aspx?q={requests.utils.quote(term)}"
        log.info(f"  Target Sports (Playwright): {url[:60]}")
        soup = fetch_js(url, wait_selector=".product-item, .product-detail", wait_ms=3500)
        if not soup: continue
        offers += _scrape_cards(
            component, soup,
            ".product-item, .product-detail, .ts-product-card",
            ".our-price, .price, .sale-price, [itemprop='price']",
            "a[href]", "Target Sports USA", "https://www.targetsportsusa.com")
    return offers

def scrape_brownells(component):
    offers = []
    for term in component.get("search_terms", [])[:1]:
        url  = f"https://www.brownells.com/search/index.htm?k={requests.utils.quote(term)}"
        soup = fetch_static(url)
        if not soup: continue
        offers += _scrape_cards(
            component, soup,
            ".js-product-card, .product-item, [data-product]",
            ".price, .js-price, [itemprop='price']",
            "a[href]", "Brownells", "https://www.brownells.com")
    return offers

VENDOR_SCRAPERS = {
    "powder_valley": scrape_powder_valley,
    "grafs":         scrape_grafs,
    "midsouth":      scrape_midsouth,
    "lucky_gunner":  scrape_lucky_gunner,
    "ammoseek":      scrape_ammoseek,
    "target_sports": scrape_target_sports,
    "brownells":     scrape_brownells,
    "ebay":          scrape_ebay,
}

# ── Trends ────────────────────────────────────────────────────────
def compute_trends(db, component_id, current_best):
    trends = {"trend_7d": None, "trend_30d": None, "alert": "hold", "avg_90d": None}
    if not db: return trends
    try:
        ref = db.collection("prices").document(component_id).collection("history")
        for days, key in [(7, "trend_7d"), (30, "trend_30d")]:
            past = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
            snap = ref.document(past).get()
            if snap.exists:
                old = snap.to_dict().get("best_per_unit")
                if old and old > 0:
                    trends[key] = round(((current_best - old) / old) * 100, 1)
        docs = ref.order_by("__name__", direction="DESCENDING").limit(90).stream()
        hist = [d.to_dict().get("best_per_unit") for d in docs if d.to_dict().get("best_per_unit")]
        if hist:
            avg = sum(hist) / len(hist)
            trends["avg_90d"] = round(avg, 6)
            if len(hist) >= 14:
                pct = ((current_best - avg) / avg) * 100
                if pct <= -5:   trends["alert"] = "buy"
                elif trends.get("trend_30d") and trends["trend_30d"] >= 10:
                    trends["alert"] = "stock_up"
    except Exception as e:
        log.warning(f"  Trend error for {component_id}: {e}")
    return trends

# ── Firebase write ────────────────────────────────────────────────
def write_to_firebase(db, comp_id, comp_name, category, offers, trends, dry_run=False):
    if not offers:
        log.warning(f"  No offers — skipping {comp_name}")
        return None
    in_stock = [o for o in offers if o.in_stock] or offers
    # Outlier filter: discard >5× median
    if len(in_stock) >= 3:
        raw_units = sorted(o.per_unit for o in in_stock)
        median    = raw_units[len(raw_units) // 2]
        filtered  = [o for o in in_stock if o.per_unit <= median * 5]
        if filtered: in_stock = filtered
    best = min(in_stock, key=lambda o: o.per_unit)
    snapshot = {
        "date":           TODAY,     "component_id":   comp_id,
        "component_name": comp_name, "category":       category,
        "best_per_unit":  best.per_unit, "best_price": best.price,
        "best_qty":       best.qty,  "best_unit":      best.unit,
        "best_vendor":    best.vendor, "best_url":     best.url,
        "offer_count":    len(offers), "last_updated":  TODAY,
        "all_offers":     [asdict(o) for o in sorted(offers, key=lambda x: x.per_unit)[:10]],
        **trends,
    }
    if dry_run:
        log.info(f"  [DRY RUN] {comp_name}: ${best.per_unit:.4f}/{best.unit} @ {best.vendor} | alert={trends.get('alert')}")
        return best
    db.collection("prices").document(comp_id).collection("history").document(TODAY).set(snapshot)
    db.collection("prices").document(comp_id).set(snapshot)
    log.info(f"  ✓ {comp_name}: ${best.per_unit:.4f}/{best.unit} @ {best.vendor} | alert={trends.get('alert')} | 7d={trends.get('trend_7d')}%")
    return best

# ── Email alerts ──────────────────────────────────────────────────
def send_alert_email(alerts):
    if not alerts: return
    frm = os.environ.get("ALERT_EMAIL_FROM")
    to  = os.environ.get("ALERT_EMAIL_TO")
    pwd = os.environ.get("ALERT_EMAIL_PASS")
    if not all([frm, to, pwd]):
        log.info("  Email alerts not configured")
        return
    buy_list   = [a for a in alerts if a["alert"] == "buy"]
    stock_list = [a for a in alerts if a["alert"] == "stock_up"]
    subject    = (f"🟢 Ammo Radar: {len(buy_list)} BUY signal(s) — {TODAY}" if buy_list
                  else f"⚠ Ammo Radar: {len(stock_list)} STOCK UP alert(s) — {TODAY}")

    def rows(items, trend_key, trend_label):
        return "".join(f"""<tr>
          <td style="padding:9px 14px;font-weight:600;">{a['name']}</td>
          <td style="padding:9px 14px;font-weight:700;">${a['per_unit']:.4f}/{a['unit']}</td>
          <td style="padding:9px 14px;">{a['vendor']}</td>
          <td style="padding:9px 14px;">{a[trend_key]:+.1f}% {trend_label}</td>
          <td style="padding:9px 14px;"><a href="{a['url']}">→ Buy</a></td>
        </tr>""" for a in items)

    def table(title, color, items, trend_key, trend_label):
        if not items: return ""
        return f"""<h2 style="color:{color};margin-top:24px;">{title}</h2>
        <table style="width:100%;border-collapse:collapse;font-size:13px;">
          <thead><tr style="background:#f8f9fa;">
            <th style="text-align:left;padding:8px 14px;">Component</th>
            <th style="text-align:left;padding:8px 14px;">Best Price</th>
            <th style="text-align:left;padding:8px 14px;">Vendor</th>
            <th style="text-align:left;padding:8px 14px;">Trend</th>
            <th style="text-align:left;padding:8px 14px;">Link</th>
          </tr></thead><tbody>{rows(items, trend_key, trend_label)}</tbody></table>"""

    body = f"""<html><body style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto;">
      <div style="background:#1a2f3a;padding:20px 28px;border-radius:10px 10px 0 0;">
        <h1 style="color:#fff;margin:0;font-size:24px;letter-spacing:2px;">🎯 Ammo Radar</h1>
        <p style="color:rgba(255,255,255,0.65);margin:4px 0 0;font-size:13px;">Daily Price Intelligence — {TODAY}</p>
      </div>
      <div style="border:1px solid #dde3e5;border-top:none;padding:24px;border-radius:0 0 10px 10px;">
        {table("🟢 Buy Now — Below 90-Day Average","#27ae60",buy_list,"trend_7d","7d")}
        {table("⚠ Stock Up — Rising Fast","#c07828",stock_list,"trend_30d","30d")}
        <p style="margin-top:24px;font-size:11px;color:#8fa8b0;">Ammo Radar · <a href="#">Open Dashboard</a></p>
      </div></body></html>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject; msg["From"] = frm; msg["To"] = to
        msg.attach(MIMEText(body, "html"))
        host = os.environ.get("ALERT_SMTP_HOST", "smtp.gmail.com")
        port = int(os.environ.get("ALERT_SMTP_PORT", "587"))
        with smtplib.SMTP(host, port) as s:
            s.ehlo(); s.starttls(); s.login(frm, pwd)
            s.sendmail(frm, to, msg.as_string())
        log.info(f"  ✉  Alert email sent to {to}")
    except Exception as e:
        log.error(f"  Email failed: {e}")

# ── Main ──────────────────────────────────────────────────────────
def run_scraper():
    args = parse_args()
    if args.verbose: log.setLevel(logging.DEBUG)
    log.info("=" * 60)
    log.info(f"Ammo Radar v3.0 — {TODAY}")
    if args.dry_run: log.info("*** DRY RUN — Firebase will NOT be written ***")
    log.info("=" * 60)

    with open(COMPONENTS_F) as f:
        config = yaml.safe_load(f)

    db = None
    try:
        db = init_firebase()
        log.info("Firebase connected ✓")
    except SystemExit:
        if not args.dry_run: raise
        log.info("Firebase not configured — running dry-run without trend data")

    cats = {
        "metals":       config.get("metals",       []),
        "powders":      config.get("powders",       []),
        "primers":      config.get("primers",       []),
        "brass":        config.get("brass",         []),
        "coatings":     config.get("coatings",      []),
        "factory_ammo": config.get("factory_ammo",  []),
    }
    if args.category:
        cats = {k: v for k, v in cats.items() if k == args.category}
    if args.component:
        cats = {k: [c for c in v if c["id"] == args.component] for k, v in cats.items()}

    stats  = {"success": 0, "no_data": 0, "error": 0}
    alerts = []

    for category, components in cats.items():
        if not components: continue
        log.info(f"\n── {category.upper()} ({len(components)}) ──")
        for comp in components:
            comp["_category"] = category
            comp_id, comp_name = comp["id"], comp["name"]
            vendors = comp.get("vendors", ["powder_valley", "grafs", "midsouth"])
            log.info(f"Scraping: {comp_name}")
            all_offers = []
            for vk in vendors:
                fn = VENDOR_SCRAPERS.get(vk)
                if not fn: continue
                try:
                    found = fn(comp)
                    if found: log.info(f"  {vk}: {len(found)} offer(s)")
                    all_offers.extend(found)
                except Exception as e:
                    log.warning(f"  {vk} error: {e}")

            if not all_offers:
                log.warning(f"  ✗ No data: {comp_name}")
                stats["no_data"] += 1
                continue

            try:
                in_stock_raw  = [o for o in all_offers if o.in_stock] or all_offers
                best_per_unit = min(o.per_unit for o in in_stock_raw)
                trends        = compute_trends(db, comp_id, best_per_unit)
                best          = write_to_firebase(db, comp_id, comp_name, category,
                                                  all_offers, trends, args.dry_run)
                if best and trends.get("alert") in ("buy","stock_up") and not args.no_email:
                    alerts.append({
                        "name": comp_name, "per_unit": best.per_unit,
                        "unit": best.unit, "vendor":   best.vendor,
                        "url":  best.url,  "alert":    trends["alert"],
                        "trend_7d":  trends.get("trend_7d")  or 0.0,
                        "trend_30d": trends.get("trend_30d") or 0.0,
                    })
                stats["success"] += 1
            except Exception as e:
                log.error(f"  ✗ Error: {comp_name}: {e}")
                stats["error"] += 1

    if alerts and not args.dry_run and not args.no_email:
        send_alert_email(alerts)
    elif alerts and args.dry_run:
        log.info(f"\n[DRY RUN] Would email {len(alerts)} alert(s):")
        for a in alerts:
            log.info(f"  {a['alert'].upper()}: {a['name']} ${a['per_unit']:.4f}/{a['unit']}")

    log.info("\n" + "=" * 60)
    log.info(f"Done — ✓{stats['success']} written  ✗{stats['no_data']} no data  "
             f"⚠{stats['error']} errors  📬{len(alerts)} alerts")
    if args.dry_run: log.info("DRY RUN complete — Firebase unchanged")
    log.info("=" * 60)

if __name__ == "__main__":
    run_scraper()
