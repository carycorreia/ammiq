#!/usr/bin/env python3
"""
AMMO IQ — Daily Price Harvester v2.6
Playwright + email alerts + dry-run mode.

Usage:
  python scraper.py                       # normal daily run
  python scraper.py --dry-run             # scrape, do NOT write to Firebase
  python scraper.py --component cci_sp    # single component
  python scraper.py --category primers    # single category
  python scraper.py --no-email            # suppress alert emails

Changelog:
  v2.6 — Title validation: validate_title() checks title_require_any, title_reject,
          title_must_also_contain_any from components.yaml for ALL vendors post-fetch.
          Qty routing: qty_unit:"count" uses parse_count_qty() for brass/primers/projectiles
          so "500 cases" → qty=500. qty_unit:"weight" keeps parse_weight_lbs() for metals.
          Both functions applied inline to eBay; applied post-fetch to other vendors.
  v2.5 — Powder Valley: added keyword relevance filter (same as Midsouth).
          Added CATEGORY_MIN_PER_UNIT price floor — powders < $15/lb, primers
          < $0.04/ea, etc. are silently dropped. Fixes $8.99 Bullseye from
          Midsouth and similar false matches that survive keyword filtering
          because "Bullseye" appears in unrelated product names.
  v2.4 — Midsouth: added keyword relevance filter — accessories at $3.99 no
          longer pollute powder/primer results. Only products whose URL or title
          contains a keyword from the search term are kept.
          Grafs: fixed link_pattern from full-domain to relative path
          (/retail/catalog/product) — price-anchor was finding prices but
          failing to match relative hrefs.
  v2.3 — All vendor URLs confirmed via live diagnostic. Grafs new URL, Lucky
          Gunner Playwright + caliber map, Rotometals BigCommerce URL,
          Midsouth SearchTerm URL, Powder Valley new domain + Playwright.
  v2.2 — Fixed Rotometals URL. Added eBay scraper. Metals → [rotometals, ebay].
  v2.1 — Fixed Midsouth fragment URL. Added 5 new vendor scrapers.
"""

import os, sys, json, re, time, logging, datetime, argparse, smtplib, asyncio
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
log = logging.getLogger("ammiq")

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

# Lucky Gunner uses per-caliber category pages, not a search endpoint.
LG_CALIBER_URLS = {
    "9mm":    "https://www.luckygunner.com/handgun/9mm-ammo",
    "45acp":  "https://www.luckygunner.com/handgun/45-acp-ammo",
    "38spl":  "https://www.luckygunner.com/handgun/38-special-ammo",
    "357mag": "https://www.luckygunner.com/handgun/357-magnum-ammo",
    "22lr":   "https://www.luckygunner.com/rimfire/22-lr-ammo",
}

# Words too generic to use as relevance keywords.
_STOP_WORDS = {
    "1lb", "1", "lb", "lbs", "powder", "box", "round", "rounds",
    "gr", "grain", "grains", "fmj", "ammo", "ammunition", "50",
    "100", "500", "1000", "pistol", "rifle", "smokeless",
}

# Minimum realistic per-unit price by category. Anything below this is a
# false match (an accessory, a target, a cleaning kit, etc.).
# per_unit = price / qty, where qty is the unit count (1 lb, 1000 primers, etc.)
CATEGORY_MIN_PER_UNIT = {
    "powders":      15.0,   # $/lb  — even economy bulk is ~$18+
    "primers":       0.04,  # $/ea  — below $40/1000 is impossible
    "metals":        0.75,  # $/lb  — lead ingots start ~$1.50/lb
    "brass":         0.05,  # $/case
    "factory_ammo":  0.10,  # $/round
    "coatings":      3.0,   # $/lb
}

# ── Data class ────────────────────────────────────────────────────
@dataclass
class PriceOffer:
    vendor:       str
    price:        float
    qty:          float
    unit:         str
    per_unit:     float
    url:          str
    in_stock:     bool  = True
    scraped_at:   str   = TODAY
    title:        str   = ""
    list_price:   float = 0.0   # original price before volume discount
    discount_pct: float = 0.0   # e.g. 10.0 for "save 10%"

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
            log.error("No Firebase credentials. Set FIREBASE_CREDENTIALS env var or add serviceAccount.json")
            sys.exit(1)
        cred = credentials.Certificate(cred_file)
    firebase_admin.initialize_app(cred, {
        "projectId": os.environ.get("FIREBASE_PROJECT_ID", "ammiq-d63b2")
    })
    return firestore.client()

# ── Helpers ───────────────────────────────────────────────────────
_PRICE_RE = re.compile(r"\$?([\d,]+\.[\d]{2})")

def parse_price(text: str) -> Optional[float]:
    """Parse a price string, handling ranges like '$40.95 - $304.95' (takes lower)."""
    text = text.strip().replace(",", "")
    parts = text.split(" - ")
    m = _PRICE_RE.search(parts[0])
    return float(m.group(1).replace(",", "")) if m else None

def get_qty(component: dict, default: float = 1.0) -> float:
    """Return the expected package quantity for per-unit price calculation.

    Priority:
      1. Explicit 'qty' key  — e.g. qty: 25 for a 25 lb metal ingot lot
      2. Numeric 'unit' key  — e.g. unit: 1000 (primers), unit: 500 (brass)
      3. default (1.0)       — e.g. unit: "lb" or unit: "oz" for powders/coatings

    This ensures:
      • Metals   (unit:"lb",  qty:25)  → 25.0  → $37.50 / 25 = $1.50/lb  ✓
      • Primers  (unit:"1000", no qty) → 1000  → $56.99 / 1000 = $0.057/ea ✓
      • Brass    (unit:"500",  no qty) → 500   → $3.99 / 500 = $0.008/case ✓
      • Powders  (unit:"lb",   no qty) → 1.0   → $24.95 / 1 = $24.95/lb  ✓
    """
    # 1. Explicit qty field takes priority
    if "qty" in component:
        try:
            v = float(component["qty"])
            if v > 0:
                return v
        except (ValueError, TypeError):
            pass
    # 2. Fall back to unit if it parses as a positive number
    try:
        v = float(component.get("unit", ""))
        if v > 0:
            return v
    except (ValueError, TypeError):
        pass
    # 3. Default
    return default


def parse_weight_lbs(text: str) -> float:
    """Extract total weight in lbs from a product title.
    Handles: '20+ Pounds', '50 + pounds', '5-lb', '1,000 lbs', '16 oz',
             '50-1 pound' (50 x 1 lb = 50 lbs), '25-2lb' (25 x 2 lb = 50 lbs)
    Returns 0.0 if nothing found."""
    text = text.lower()

    # Pattern: "50-1 pound" or "25-2lb" → N x W lb format (count dash weight)
    m = re.search(r'(\d+)\s*[-x]\s*(\d+(?:\.\d+)?)\s*[-]?\s*(?:lbs?|pounds?)', text)
    if m:
        count, weight = float(m.group(1)), float(m.group(2))
        # Only treat as count×weight if count looks like a quantity (>1) and weight is small
        if count > 1 and weight <= 50:
            return count * weight

    # Standard: "36 lb", "50 + pounds", "20+ lbs", "1,000 lbs"
    m = re.search(r'([\d,]+(?:\.\d+)?)\s*\+?\s*[-]?\s*(?:lbs?|pounds?)', text)
    if m:
        return float(m.group(1).replace(',', ''))

    # Ounces
    m = re.search(r'([\d,]+(?:\.\d+)?)\s*\+?\s*[-]?\s*oz\b', text)
    if m:
        return round(float(m.group(1).replace(',', '')) / 16, 4)

    return 0.0

def validate_title(title: str, component: dict) -> bool:
    """
    Return True if the product title passes all three filters from components.yaml:
      1. title_require_any          — title must contain AT LEAST ONE of these strings
      2. title_reject               — title must NOT contain ANY of these strings
      3. title_must_also_contain_any — secondary positive gate (e.g. brass must say "brass"/"case")
    All checks are case-insensitive.
    Returns True (pass) when a filter list is absent or empty.
    """
    t = title.lower()

    # 1. Positive gate
    require_any = component.get("title_require_any", [])
    if require_any and not any(r.lower() in t for r in require_any):
        return False

    # 2. Reject list
    for bad in component.get("title_reject", []):
        if bad.lower() in t:
            return False

    # 3. Secondary positive gate
    must_also = component.get("title_must_also_contain_any", [])
    if must_also and not any(m.lower() in t for m in must_also):
        return False

    return True


def parse_count_qty(title: str) -> float:
    """
    Extract item COUNT from a product title (brass, primers, projectiles).
    Handles:
      "500 cases", "1,000ct", "250 pieces", "100 rounds", "500/box",
      "1000 count", "box of 100", "500pk", "250-count", "1k rounds"
    Returns 0.0 if nothing found.
    """
    t = title.lower()

    # "box of N" / "pack of N" / "bag of N"
    m = re.search(r'(?:box|pack|bag)\s+of\s+([\d,]+)', t)
    if m:
        return float(m.group(1).replace(',', ''))

    # "N count", "N ct", "N/box", "N pieces", "N rounds", "N cases", "N casings", "N pk"
    m = re.search(
        r'([\d,]+)\s*[-/]?\s*'
        r'(?:count\b|ct\b|pieces?\b|pcs?\b|rounds?\b|cases?\b|casings?\b|pk\b|pack\b)',
        t,
    )
    if m:
        return float(m.group(1).replace(',', ''))

    # "Nk" shorthand: "1k rounds" → 1000, "2k" → 2000
    m = re.search(r'(\d+(?:\.\d+)?)\s*k\b', t)
    if m:
        return float(m.group(1)) * 1000

    return 0.0


def expand_volume_tiers(product_url: str, base_offer: "PriceOffer", component: dict) -> list:
    """
    Fetch an individual product page and look for a volume discount / quantity
    break table.  Returns a list of PriceOffer objects — one per price tier.
    Falls back to [base_offer] if no table is found or the fetch fails.

    Handles table patterns like:
      Rounds | Units | Savings | Price per Unit | Total Price
      100    |  1    | <none>  | $26.29         | $26.29
      500    |  5    | Save 5% | $24.98         | $124.88

    Also handles simple "X for $Y" discount rows in Starline-style product pages.
    """
    try:
        soup = fetch_static(product_url)
        if not soup:
            return [base_offer]

        qty_unit_field = component.get("qty_unit", "weight")
        unit_label     = str(component.get("unit", "lb"))

        # ── Strategy 1: table with a "Price per Unit" header ────────────
        # Find any <table> whose header row contains "price" and either
        # "unit" or "rounds" — typical volume discount layout.
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            if not headers:
                headers = [td.get_text(strip=True).lower()
                           for td in (table.find("tr") or soup.new_tag("x")).find_all("td")]
            has_price = any("price" in h for h in headers)
            has_qty   = any(k in " ".join(headers) for k in ("round", "unit", "count", "qty"))
            if not has_price or not has_qty:
                continue

            # Identify column positions
            price_col = next((i for i,h in enumerate(headers) if "price per" in h or ("price" in h and "total" not in h)), None)
            qty_col   = next((i for i,h in enumerate(headers) if any(k in h for k in ("round","count","qty"))), 0)
            total_col = next((i for i,h in enumerate(headers) if "total" in h), None)

            tiers = []
            for row in table.find_all("tr")[1:]:   # skip header
                cells = row.find_all(["td","th"])
                if len(cells) < 2:
                    continue
                # qty of items (rounds/cases)
                qty_text = cells[qty_col].get_text(strip=True).replace(",","") if qty_col < len(cells) else ""
                qty_val  = 0.0
                if qty_unit_field == "count":
                    try: qty_val = float(re.sub(r"[^\d.]","",qty_text))
                    except: pass
                else:
                    qty_val = parse_weight_lbs(qty_text) or base_offer.qty

                # per-unit price
                if price_col is not None and price_col < len(cells):
                    pu = parse_price(cells[price_col].get_text())
                else:
                    pu = None

                # total price
                total_price = None
                if total_col is not None and total_col < len(cells):
                    total_price = parse_price(cells[total_col].get_text())

                if not pu and total_price and qty_val:
                    pu = total_price / qty_val

                if not pu or not qty_val:
                    continue

                # Savings badge text (e.g. "Save 5%")
                savings_text = ""
                for cell in cells:
                    t = cell.get_text(strip=True)
                    if "save" in t.lower() or "%" in t:
                        savings_text = t
                        break

                disc_pct = 0.0
                m = re.search(r"(\d+(?:\.\d+)?)\s*%", savings_text)
                if m:
                    disc_pct = float(m.group(1))

                # total landed cost = total_price + base shipping
                # keep shipping proportional — use same flat shipping as base offer
                ship_contribution = base_offer.price - base_offer.per_unit * base_offer.qty
                landed = (total_price or pu * qty_val) + max(ship_contribution, 0)

                tiers.append(PriceOffer(
                    vendor=base_offer.vendor,
                    price=round(landed, 4),
                    qty=qty_val,
                    unit=unit_label,
                    per_unit=round(pu, 6),
                    url=product_url,
                    in_stock=base_offer.in_stock,
                    scraped_at=TODAY,
                    title=base_offer.title or "",
                    list_price=base_offer.list_price,
                    discount_pct=disc_pct,
                ))

            if tiers:
                log.debug(f"  Volume tiers found on {product_url[:60]}: {len(tiers)} tier(s)")
                return tiers

        # ── Strategy 2: look for quantity-select option tags ─────────────
        # Some sites use <select> or radio buttons for qty breaks.
        offers = []
        for opt in soup.select("select.qty option, select[name*='qty'] option, select[name*='pack'] option"):
            val  = opt.get_text(strip=True)
            data = opt.get("data-price") or opt.get("value", "")
            qty  = parse_count_qty(val) if qty_unit_field == "count" else parse_weight_lbs(val)
            pr   = parse_price(val) or (parse_price(str(data)) if data else None)
            if qty and pr:
                offers.append(PriceOffer(
                    vendor=base_offer.vendor, price=pr, qty=qty, unit=unit_label,
                    per_unit=round(pr/qty, 6), url=product_url,
                    in_stock=base_offer.in_stock, scraped_at=TODAY,
                    title=base_offer.title or "",
                ))
        if offers:
            return offers

    except Exception as e:
        log.debug(f"  Volume tier expansion failed for {product_url[:60]}: {e}")

    return [base_offer]


def first_el(card, selectors: list):
    for sel in selectors:
        el = card.select_one(sel)
        if el:
            return el
    return None

def keywords_for(term: str) -> set:
    """Return meaningful keywords from a search term, dropping stop words."""
    return {w.lower() for w in term.split() if w.lower() not in _STOP_WORDS}

def is_relevant(text: str, keywords: set) -> bool:
    """Return True if text contains at least one of the keywords."""
    if not keywords:
        return True
    text = text.lower()
    return any(kw in text for kw in keywords)

def fetch_static(url: str) -> Optional[BeautifulSoup]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        time.sleep(DELAY)
        return BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        log.warning(f"  Static fetch failed: {e}")
        return None

async def _fetch_js(url: str, wait_selector: str = None, wait_ms: int = 4000):
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx  = await browser.new_context(user_agent=HEADERS["User-Agent"], locale="en-US")
            page = await ctx.new_page()
            await page.goto(url, timeout=30000)
            if wait_selector:
                try:
                    await page.wait_for_selector(wait_selector, timeout=10000)
                except Exception:
                    pass
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

def fetch_js(url: str, wait_selector: str = None, wait_ms: int = 4000) -> Optional[BeautifulSoup]:
    return asyncio.run(_fetch_js(url, wait_selector, wait_ms))

# ── Per-unit sanity caps ($/unit). Offers outside these bounds are discarded. ──
_SANITY_CAPS = {
    "factory_ammo": (0.01, 2.00),   # $0.01–$2.00 per round
    "primers":      (0.01, 0.30),   # $0.01–$0.30 each
    "brass":        (0.01, 2.00),   # $0.01–$2.00 per case
    "projectiles":  (0.01, 2.00),
    "powder":       (10.0, 500.0),  # per lb
    "metals":       (0.50, 30.0),   # per lb
}

def sanity_per_unit(per_unit: float, category: str) -> bool:
    """Return True if per_unit is within a plausible range for the category."""
    lo, hi = _SANITY_CAPS.get(category, (0.0, 1e9))
    return lo <= per_unit <= hi


def _extract_title(container) -> str:
    """Pull product title text from a DOM container."""
    for sel in ["h2", "h3", "h4", ".product-name", ".product-title",
                ".item-name", "[class*='title']", "[class*='name']"]:
        el = container.select_one(sel) if hasattr(container, "select_one") else None
        if el:
            return el.get_text(" ", strip=True)
    # Fall back to the link text
    a = container.find("a") if hasattr(container, "find") else None
    return a.get_text(" ", strip=True) if a else ""


def price_anchor_offers(soup, vendor_name, component, link_domain,
                        price_selector=None, link_pattern=None, max_results=5):
    """
    Find products by locating price elements and walking up the DOM to find
    the nearest ancestor containing a product link. Used for sites with
    non-standard product card markup (Grafs, Lucky Gunner).

    FIX v2.7:
    - Extracts product title from each card container
    - Filters by brand/caliber match in TITLE (not just URL)
    - Parses round/unit count from title when qty_unit=count, overriding
      the component's hardcoded unit value — fixes the $225/50 = $4.50/round bug
    - Discards offers whose per_unit falls outside sanity caps
    """
    offers   = []
    seen     = set()
    brand    = (component.get("brand") or "").lower()
    caliber  = (component.get("caliber") or "").lower()
    category = component.get("_category", "")
    qty_unit = component.get("qty_unit", "")

    if price_selector:
        price_els = soup.select(price_selector)
    else:
        price_els = [
            el.parent for el in soup.find_all(
                string=re.compile(r"^\$[\d,]+\.[\d]{2}$")
            )
        ]

    for price_el in price_els:
        price = parse_price(price_el.get_text())
        if not price:
            continue

        container = price_el
        link_el   = None
        for _ in range(10):
            container = container.parent
            if not container or container.name in ("html", "body", "[document]"):
                break
            pattern   = link_pattern or r"."
            candidates = container.find_all("a", href=re.compile(pattern))
            if candidates:
                link_el = candidates[0]
                break

        if not link_el:
            continue

        href = link_el["href"]
        if not href.startswith("http"):
            href = link_domain.rstrip("/") + "/" + href.lstrip("/")
        if href in seen:
            continue
        seen.add(href)

        # ── Title extraction & brand/caliber filter ───────────────────────────
        title_text = _extract_title(container) or link_el.get_text(" ", strip=True)
        title_low  = title_text.lower()

        if brand and brand not in title_low:
            log.debug(f"  {vendor_name}: skip (brand mismatch) — {title_text[:60]}")
            continue
        if caliber and caliber not in title_low.replace("-", "").replace(" ", ""):
            # allow "9mm" to match "9 mm", "22lr" to match "22 long rifle" etc
            cal_variants = {caliber, caliber.replace("lr","long rifle"),
                            caliber.replace("acp","auto"), caliber.replace("spl","special")}
            if not any(v in title_low for v in cal_variants):
                log.debug(f"  {vendor_name}: skip (caliber mismatch) — {title_text[:60]}")
                continue

        # ── Qty: parse from title when qty_unit=count, else use component default ──
        if qty_unit == "count":
            parsed = parse_count_qty(title_text)
            qty    = parsed if parsed and parsed > 1 else get_qty(component)
        else:
            qty = get_qty(component)

        per_unit = round(price / qty, 6) if qty else price

        # ── Sanity check ────────────────────────────────────────────────────────
        if category and not sanity_per_unit(per_unit, category):
            log.warning(f"  {vendor_name}: sanity fail ${per_unit:.4f}/{category} "
                        f"(price=${price}, qty={qty}) — {title_text[:50]}")
            continue

        offers.append(PriceOffer(
            vendor_name, price, qty,
            str(component.get("unit", "1")),
            per_unit, href,
        ))
        if len(offers) >= max_results:
            break

    return offers


# ── Vendor scrapers ───────────────────────────────────────────────

def scrape_powder_valley(component):
    """
    Powder Valley — powdervalley.com (domain changed from powdervalleyinc.com).
    JS-rendered via Algolia search widget. Card: li.ais-Hits-item.product.
    Price may be a range ("$40.95 - $304.95") — parse_price() takes the lower.
    FIX v2.5: Added keyword relevance filter — same as Midsouth fix. Algolia
    returns 10 results including accessories; only keep cards whose link href
    or title text contains a keyword from the search term.
    """
    offers = []
    for term in component.get("search_terms", [])[:2]:
        url  = f"https://www.powdervalley.com/search/?q={requests.utils.quote(term)}"
        log.info(f"  Powder Valley (Playwright): {url[:70]}")
        soup = fetch_js(url, wait_selector="li.ais-Hits-item, li.product", wait_ms=2500)
        if not soup:
            continue
        kws   = keywords_for(term)
        cards = soup.select("li.ais-Hits-item.product, li.ais-Hits-item, li.product")
        for card in cards[:10]:
            price_el = first_el(card, [".price", ".amount", "span.price"])
            link_el  = card.select_one("a[href*='/product/'], a.prod-img-w, a[href]")
            if not price_el or not link_el:
                continue
            href       = link_el["href"]
            title_attr = link_el.get("title", "") or link_el.get_text(strip=True)
            check_text = (href + " " + title_attr).lower()
            if not is_relevant(check_text, kws):
                continue
            price = parse_price(price_el.get_text())
            if not price:
                continue
            qty  = get_qty(component)
            if href.startswith("/"):
                href = "https://www.powdervalley.com" + href
            offers.append(PriceOffer(
                "Powder Valley", price, qty,
                str(component.get("unit", "1")),
                round(price / qty, 6) if qty else price, href,
            ))
    return offers


def scrape_grafs(component):
    """
    Graf & Sons — /retail/catalog/search?keywords=<term>
    Uses price-anchor strategy. FIX v2.4: link_pattern now matches relative
    hrefs (/retail/catalog/product/...) instead of requiring the full domain,
    which was why price-anchor was finding prices but returning no offers.
    FIX v2.6: expand_volume_tiers() fetches each product page and stores every
    price-break row as a separate PriceOffer.
    """
    offers = []
    for term in component.get("search_terms", [])[:2]:
        url  = f"https://www.grafs.com/retail/catalog/search?keywords={requests.utils.quote(term)}"
        soup = fetch_static(url)
        if not soup:
            continue
        found = price_anchor_offers(
            soup, "Grafs", component,
            link_domain="https://www.grafs.com",
            link_pattern=r"/retail/catalog/product",
        )
        # Expand volume discount tiers — only for count-unit components (brass/primers)
        # and only for the first 3 product pages to keep runtime bounded.
        if component.get("qty_unit") == "count":
            expanded = []
            for base_offer in found[:3]:
                tiers = expand_volume_tiers(base_offer.url, base_offer, component)
                expanded.extend(tiers)
            # Keep any remaining base offers we didn't expand
            expanded.extend(found[3:])
            offers.extend(expanded if expanded else found)
        else:
            offers.extend(found)
    return offers


def scrape_midsouth(component):
    """
    Midsouth Shooters Supply — /search?SearchTerm=<term>, Playwright.
    FIX v2.4: Added keyword relevance filter. Midsouth search returns ~12
    results including unrelated accessories at $3.99. Only products whose
    URL or title contains a keyword from the search term are kept.
    Confirmed working: Titegroup 1lb=$41.99, 5lb=$159.99, 8lb=$313.99.
    """
    offers = []
    for term in component.get("search_terms", [])[:2]:
        url  = f"https://www.midsouthshooterssupply.com/search?SearchTerm={requests.utils.quote(term)}"
        log.info(f"  Midsouth (Playwright): {url[:70]}")
        soup = fetch_js(url, wait_selector=".product-wrapper, .product", wait_ms=3000)
        if not soup:
            continue

        # Keywords for relevance filtering (e.g. "hodgdon titegroup 1lb" → {"hodgdon","titegroup"})
        kws = keywords_for(term)

        cards = soup.select(".product")
        for card in cards[:8]:
            link_el = card.select_one("a[href*='/item/'], a[href]")
            if not link_el:
                continue

            # Relevance check: product URL or title must contain a keyword
            href  = link_el["href"]
            title_attr = link_el.get("title", "")
            check_text = (href + " " + title_attr).lower()
            if not is_relevant(check_text, kws):
                continue

            # Find price: first <span> whose full text is a price
            price_el = None
            for span in card.find_all("span"):
                t = span.get_text(strip=True)
                if t.startswith("$") and _PRICE_RE.match(t):
                    price_el = span
                    break

            if not price_el:
                continue
            price = parse_price(price_el.get_text())
            if not price:
                continue

            qty = get_qty(component)
            if href.startswith("/"):
                href = "https://www.midsouthshooterssupply.com" + href
            offers.append(PriceOffer(
                "Midsouth", price, qty,
                str(component.get("unit", "1")),
                round(price / qty, 6) if qty else price, href,
            ))
    return offers


def scrape_lucky_gunner(component):
    """
    Lucky Gunner — JS-rendered, Playwright. Caliber category pages only.
    No search endpoint exists. Only works for components with a 'caliber' field.
    Price elements: span.price and span.regular-price (confirmed via diagnostic).
    """
    caliber = component.get("caliber", "")
    if not caliber:
        log.warning("  Lucky Gunner skipped — no 'caliber' field (LG only carries factory ammo)")
        return []

    url = LG_CALIBER_URLS.get(caliber)
    if not url:
        log.warning(f"  Lucky Gunner: no URL mapped for caliber '{caliber}'")
        return []

    log.info(f"  Lucky Gunner (Playwright): {url}")
    soup = fetch_js(url, wait_selector="span.price, span.regular-price", wait_ms=3000)
    if not soup:
        return []

    brand = component.get("brand", "").lower()
    kws   = set()
    for term in component.get("search_terms", []):
        kws |= keywords_for(term)
    if brand:
        kws.add(brand.lower())

    offers = price_anchor_offers(
        soup, "Lucky Gunner", component,
        link_domain="https://www.luckygunner.com",
        price_selector="span.price, span.regular-price",
        link_pattern=r"luckygunner\.com/",
        max_results=8,
    )

    # Filter to products matching the tracked brand
    if brand and offers:
        filtered = [o for o in offers if is_relevant(o.url, {brand})]
        if filtered:
            return filtered

    return offers[:3]


def scrape_ammoseek(component):
    """AmmoSeek — JS-rendered aggregator, Playwright."""
    caliber = component.get("caliber", "")
    if not caliber:
        return []
    cal_map = {
        "9mm": "9mm-luger", "45acp": "45-auto", "38spl": "38-special",
        "357mag": "357-magnum", "22lr": "22-long-rifle",
    }
    slug  = cal_map.get(caliber, caliber.replace(" ", "-"))
    grain = component.get("grain", "")
    url   = f"https://ammoseek.com/ammo/{slug}" + (f"?gr={grain}" if grain else "")
    log.info(f"  AmmoSeek (Playwright): {url}")
    soup  = fetch_js(url, wait_selector="tr.offer-row, .listing-item", wait_ms=3000)
    if not soup:
        return []
    offers = []
    for row in soup.select("tr.offer-row, .listing-item, .ammo-row")[:10]:
        price_el  = first_el(row, [".price-per-round", ".cpr", "td.cpr", "[data-cpr]"])
        vendor_el = first_el(row, [".retailer", ".vendor-name", "td.vendor", ".seller"])
        link_el   = row.select_one("a[href]")
        stock_el  = row.select_one(".stock, .availability")
        if not price_el:
            continue
        cpr      = parse_price(price_el.get_text())
        if not cpr:
            continue
        box_qty  = get_qty(component, 50.0)
        in_stock = "out" not in (stock_el.get_text().lower() if stock_el else "in")
        offers.append(PriceOffer(
            vendor_el.get_text().strip() if vendor_el else "AmmoSeek",
            round(cpr * box_qty, 4), box_qty,
            str(component.get("unit", "50")),
            cpr, link_el["href"] if link_el else url, in_stock,
        ))
    return offers


def scrape_target_sports(component):
    """Target Sports USA — JS-rendered, Playwright."""
    offers = []
    for term in component.get("search_terms", [])[:1]:
        url  = f"https://www.targetsportsusa.com/search.aspx?q={requests.utils.quote(term)}"
        log.info(f"  Target Sports (Playwright): {url[:65]}")
        soup = fetch_js(url, wait_selector=".product-item, .product-detail", wait_ms=2500)
        if not soup:
            continue
        cards = soup.select(".ts-product-card, .product-item, .product-detail")
        for card in cards[:5]:
            price_el = first_el(card, [".our-price", ".sale-price", "[itemprop='price']", ".price"])
            link_el  = card.select_one("a[href]")
            if not price_el:
                continue
            raw   = price_el.get("content") or price_el.get_text()
            price = parse_price(str(raw))
            if not price:
                continue
            qty = get_qty(component, 50.0)
            offers.append(PriceOffer(
                "Target Sports USA", price, qty,
                str(component.get("unit", "50")),
                round(price / qty, 6) if qty else price,
                link_el["href"] if link_el else url,
            ))
    return offers


def scrape_brownells(component):
    """Brownells — React SPA, Playwright."""
    offers = []
    for term in component.get("search_terms", [])[:1]:
        url  = f"https://www.brownells.com/search/index.htm?k={requests.utils.quote(term)}"
        log.info(f"  Brownells (Playwright): {url[:65]}")
        soup = fetch_js(url, wait_selector=".js-product-card, .product-item", wait_ms=3000)
        if not soup:
            continue
        cards = soup.select(".js-product-card, .product-item, [data-product]")
        for card in cards[:5]:
            price_el = first_el(card, [".js-price", "[itemprop='price']", ".price"])
            link_el  = card.select_one("a[href]")
            if not price_el:
                continue
            raw   = price_el.get("content") or price_el.get_text()
            price = parse_price(str(raw))
            if not price:
                continue
            qty  = get_qty(component)
            href = link_el["href"] if link_el else url
            if href.startswith("/"):
                href = "https://www.brownells.com" + href
            offers.append(PriceOffer(
                "Brownells", price, qty,
                str(component.get("unit", "1")),
                round(price / qty, 6) if qty else price, href,
            ))
    return offers


def scrape_rotometals(component):
    """
    Rotometals — BigCommerce. Confirmed URL, card, and price selectors.
    URL:   /search.php?search_query=<term>&section=product
    Card:  li.product > article.card
    Price: span.price--withoutTax.price--main
    """
    offers = []
    for term in component.get("search_terms", [])[:2]:
        url  = f"https://www.rotometals.com/search.php?search_query={requests.utils.quote(term)}&section=product"
        soup = fetch_static(url)
        if not soup:
            continue
        for card in soup.select("li.product")[:5]:
            price_el = first_el(card, [
                ".price--withoutTax.price--main",
                ".price.price--main",
                ".price--withoutTax",
                ".price",
            ])
            link_el = card.select_one("article.card a[href], a[href]")
            if not price_el or not link_el:
                continue
            price = parse_price(price_el.get_text())
            if not price:
                continue
            # Try to get weight from product title — more reliable than qty
            # field alone (e.g. "Pure Lead Ingot 25 Lbs" → 25.0)
            title_el = card.select_one(".card-title, .card-body .card-title, h4, h3, a[aria-label]")
            title_text = title_el.get_text() if title_el else ""
            title_lbs = parse_weight_lbs(title_text)
            qty = title_lbs if title_lbs >= 1.0 else get_qty(component)
            href = link_el["href"]
            if href.startswith("/"):
                href = "https://www.rotometals.com" + href
            offers.append(PriceOffer(
                "Rotometals", price, qty,
                str(component.get("unit", "lb")),
                round(price / qty, 6) if qty else price, href,
                title=title_text,
            ))
    return offers


def _ebay_token() -> str:
    """Get eBay Browse API OAuth token via Client Credentials."""
    app_id  = os.environ.get("EBAY_APP_ID", "")
    cert_id = os.environ.get("EBAY_CERT_ID", "")
    if not app_id or not cert_id:
        return ""
    try:
        import base64
        creds = base64.b64encode(f"{app_id}:{cert_id}".encode()).decode()
        r = requests.post(
            "https://api.ebay.com/identity/v1/oauth2/token",
            headers={"Authorization": f"Basic {creds}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data="grant_type=client_credentials&scope=https%3A%2F%2Fapi.ebay.com%2Foauth%2Fapi_scope",
            timeout=15,
        )
        data = r.json()
        token = data.get("access_token", "")
        if not token:
            logging.warning(f"eBay token request failed — HTTP {r.status_code} — {data}")
        return token
    except Exception as e:
        logging.warning(f"eBay token error: {e}")
        return ""

_EBAY_TOKEN = None

def scrape_ebay(component):
    """eBay Browse API — OAuth, Buy It Now, sorted by price asc."""
    global _EBAY_TOKEN
    if _EBAY_TOKEN is None:
        _EBAY_TOKEN = _ebay_token()
    if not _EBAY_TOKEN:
        logging.warning("eBay: no OAuth token — skipping")
        return []

    offers = []
    unit   = str(component.get("unit", "lb"))

    seen_ids: set = set()
    for term in component.get("search_terms", []):
        offset = 0
        PAGE   = 200   # eBay API max per request
        while True:
            try:
                url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
                params = {
                    "q":      term,
                    "filter": "buyingOptions:{FIXED_PRICE}",
                    "sort":   "bestMatch",
                    "limit":  str(PAGE),
                    "offset": str(offset),
                }
                r = requests.get(
                    url, params=params,
                    headers={"Authorization": f"Bearer {_EBAY_TOKEN}",
                             "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
                             "Content-Type": "application/json"},
                    timeout=20,
                )
                if r.status_code == 401:
                    _EBAY_TOKEN = _ebay_token()
                    continue
                if not r.ok:
                    logging.warning(f"eBay API {r.status_code} for '{term}' offset={offset}")
                    break

                data  = r.json()
                items = data.get("itemSummaries", [])
                total = int(data.get("total", 0))
                if not items:
                    break

                for item in items:
                    # Dedup — same listing can appear across multiple search terms/pages
                    item_id = item.get("itemId", "")
                    if item_id and item_id in seen_ids:
                        continue
                    if item_id:
                        seen_ids.add(item_id)

                    title      = item.get("title", "")
                    price_info = item.get("price", {})
                    ship_info  = (item.get("shippingOptions") or [{}])[0]
                    list_price = float(price_info.get("value", 0) or 0)
                    price      = list_price

                    # Title validation — skip gauges, shell plates, accessories, etc.
                    if not validate_title(title, component):
                        logging.debug(f"eBay title rejected: {title[:80]}")
                        continue

                    # Volume discount ("Save X% when you buy more")
                    disc_info  = item.get("discountPricingInfo", {})
                    disc_pct   = float((disc_info.get("discountPercentage") or 0) or 0)
                    disc_amt   = float((disc_info.get("discountAmount") or {}).get("value", 0) or 0)
                    if disc_amt and disc_amt < price:
                        price = disc_amt

                    ship = float((ship_info.get("shippingCost") or {}).get("value", 0) or 0)
                    price += ship   # total landed cost
                    if not price:
                        continue
                    url_item = item.get("itemWebUrl", "")

                    # Route qty parsing by qty_unit field
                    qty_unit_field = component.get("qty_unit", "weight")
                    if qty_unit_field == "count":
                        # Brass, primers, projectiles — parse item count from title
                        parsed_qty = parse_count_qty(title)
                        if parsed_qty >= 1:
                            qty = parsed_qty
                        else:
                            qty = get_qty(component)
                    else:
                        # Metals and powders — parse lot weight from title
                        title_lbs = parse_weight_lbs(title)
                        if title_lbs >= 0.5:
                            qty = title_lbs
                        else:
                            qty = get_qty(component)

                    per_unit = round(price / qty, 6) if qty else price

                    # Sanity check — skip wildly overpriced per-unit
                    ceiling = component.get("price_ceiling", 999)
                    if per_unit > ceiling:
                        continue

                    offers.append(PriceOffer(
                        vendor="eBay", price=price, qty=qty, unit=unit,
                        per_unit=per_unit, url=url_item,
                        title=title, list_price=list_price, discount_pct=disc_pct,
                    ))

                offset += PAGE
                if offset >= total:
                    break   # no more pages

            except Exception as e:
                logging.warning(f"eBay Browse API error for '{term}' offset={offset}: {e}")
                break   # stop paginating this term on error
    return offers


def scrape_starline(component):
    """Starline Brass — unprimed brass cases, static HTML."""
    offers = []
    for term in component.get("search_terms", [])[:2]:
        url  = f"https://www.starlinebrass.com/search/?q={requests.utils.quote(term)}"
        soup = fetch_static(url)
        if not soup:
            continue
        for card in soup.select(".product-card, .product-item, .product, li.product")[:5]:
            price_el = first_el(card, [
                ".product-price", ".woocommerce-Price-amount", ".price bdi", ".price",
            ])
            link_el  = card.select_one("a[href*='/brass/'], a[href*='/products/'], a[href]")
            stock_el = card.select_one(".stock, .in-stock, .out-of-stock, .availability")
            title_el = card.select_one(".product-title, h2, h3, .woocommerce-loop-product__title, a[aria-label]")
            if not price_el:
                continue
            price      = parse_price(price_el.get_text())
            if not price:
                continue
            title_text = title_el.get_text(strip=True) if title_el else ""
            in_stock   = "out" not in (stock_el.get_text().lower() if stock_el else "in")
            qty_unit_field = component.get("qty_unit", "weight")
            if qty_unit_field == "count":
                parsed_count = parse_count_qty(title_text)
                qty = parsed_count if parsed_count >= 1 else get_qty(component, 500.0)
            else:
                qty = get_qty(component, 500.0)
            href     = link_el["href"] if link_el else url
            if href.startswith("/"):
                href = "https://www.starlinebrass.com" + href
            base = PriceOffer(
                "Starline", price, qty,
                str(component.get("unit", "500")),
                round(price / qty, 6) if qty else price, href, in_stock,
                title=title_text,
            )
            # Expand volume discount tiers for count-unit components only
            if component.get("qty_unit") == "count":
                tiers = expand_volume_tiers(href, base, component)
                offers.extend(tiers)
            else:
                offers.append(base)
    return offers


def scrape_magnus_bullets(component):
    """Magnus Bullets — WooCommerce, Hi-Tek coated cast bullets."""
    offers = []
    for term in component.get("search_terms", [])[:2]:
        url  = f"https://www.magnusbullets.com/?s={requests.utils.quote(term)}&post_type=product"
        soup = fetch_static(url)
        if not soup:
            continue
        for card in soup.select("li.product, .product, .product-small")[:5]:
            price_el = first_el(card, [
                ".woocommerce-Price-amount bdi", ".woocommerce-Price-amount",
                ".price ins .amount", ".price",
            ])
            link_el = card.select_one("a[href]")
            if not price_el:
                continue
            price = parse_price(price_el.get_text())
            if not price:
                continue
            qty = get_qty(component)
            offers.append(PriceOffer(
                "Magnus Bullets", price, qty,
                str(component.get("unit", "lb")),
                round(price / qty, 6) if qty else price,
                link_el["href"] if link_el else url,
            ))
    return offers


def scrape_bayou_bullets(component):
    """Bayou Bullets — WooCommerce, Hi-Tek coated cast bullets."""
    offers = []
    for term in component.get("search_terms", [])[:2]:
        url  = f"https://bayoubullets.com/?s={requests.utils.quote(term)}&post_type=product"
        soup = fetch_static(url)
        if not soup:
            continue
        for card in soup.select("li.product, .product, .product-small")[:5]:
            price_el = first_el(card, [
                ".woocommerce-Price-amount bdi", ".woocommerce-Price-amount",
                ".price ins .amount", ".price",
            ])
            link_el = card.select_one("a[href]")
            if not price_el:
                continue
            price = parse_price(price_el.get_text())
            if not price:
                continue
            qty = get_qty(component)
            offers.append(PriceOffer(
                "Bayou Bullets", price, qty,
                str(component.get("unit", "lb")),
                round(price / qty, 6) if qty else price,
                link_el["href"] if link_el else url,
            ))
    return offers


def scrape_harbor_freight(component):
    """Harbor Freight — powder coat. May be blocked by Cloudflare."""
    offers = []
    for term in component.get("search_terms", [])[:1]:
        url  = f"https://www.harborfreight.com/catalogsearch/result/?q={requests.utils.quote(term)}"
        soup = fetch_static(url)
        if not soup:
            continue
        title_tag = soup.find("title")
        if title_tag and "just a moment" in title_tag.get_text().lower():
            log.warning("  Harbor Freight: Cloudflare challenge — scraper blocked.")
            return []
        for card in soup.select("[data-qa='product-card'], .grid-item, .product-card")[:5]:
            price_el = first_el(card, [".price-current", "[data-qa='price']", ".price"])
            link_el  = card.select_one("a[href]")
            if not price_el:
                continue
            price = parse_price(price_el.get_text())
            if not price:
                continue
            qty  = get_qty(component)
            href = link_el["href"] if link_el else url
            if href.startswith("/"):
                href = "https://www.harborfreight.com" + href
            offers.append(PriceOffer(
                "Harbor Freight", price, qty,
                str(component.get("unit", "lb")),
                round(price / qty, 6) if qty else price, href,
            ))
    return offers


def scrape_amazon(component):
    """Amazon — blocked by bot detection. Remove from components.yaml vendors."""
    log.warning("  Amazon scraping is blocked. Remove 'amazon' from vendors in components.yaml.")
    return []


# ── Vendor registry ───────────────────────────────────────────────
VENDOR_SCRAPERS = {
    "powder_valley":  scrape_powder_valley,
    "grafs":          scrape_grafs,
    "midsouth":       scrape_midsouth,
    "lucky_gunner":   scrape_lucky_gunner,
    "ammoseek":       scrape_ammoseek,
    "target_sports":  scrape_target_sports,
    "brownells":      scrape_brownells,
    "rotometals":     scrape_rotometals,
    "ebay":           scrape_ebay,
    "starline":       scrape_starline,
    "magnus_bullets": scrape_magnus_bullets,
    "bayou_bullets":  scrape_bayou_bullets,
    "harbor_freight": scrape_harbor_freight,
    "amazon":         scrape_amazon,
}

# ── Trends ────────────────────────────────────────────────────────
def compute_trends(db, component_id, current_best):
    trends = {"trend_7d": None, "trend_30d": None, "alert": "hold", "avg_90d": None}
    if not db:
        return trends
    try:
        ref = db.collection("prices").document(component_id).collection("history")
        for days, key in [(7, "trend_7d"), (30, "trend_30d")]:
            past = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
            snap = ref.document(past).get()
            if snap.exists:
                old = snap.to_dict().get("best_per_unit")
                if old and old > 0:
                    trends[key] = round(((current_best - old) / old) * 100, 1)
        docs = ref.stream()
        all_hist = sorted(
            [d.to_dict() for d in docs],
            key=lambda x: x.get("date", ""),
            reverse=True,
        )[:90]
        hist = [d.get("best_per_unit") for d in all_hist if d.get("best_per_unit")]
        if hist:
            avg = sum(hist) / len(hist)
            trends["avg_90d"] = round(avg, 6)
            if len(hist) >= 14:
                pct = ((current_best - avg) / avg) * 100
                if pct <= -5:
                    trends["alert"] = "buy"
                elif trends.get("trend_30d") and trends["trend_30d"] >= 10:
                    trends["alert"] = "stock_up"
    except Exception as e:
        log.warning(f"  Trend error for {component_id}: {e}")
    return trends

# ── Firebase write ────────────────────────────────────────────────
def write_to_firebase(db, comp_id, comp_name, category, offers, trends, dry_run=False,
                      component=None):
    if not offers:
        log.warning(f"  No offers — skipping {comp_name}")
        return None
    in_stock = [o for o in offers if o.in_stock] or offers

    # Price ceiling — drop unrealistically expensive results before picking best
    _CEIL = {
        "metals":       12.0,   # raw lead $1.50-4, Lyman #2 ~$6, Linotype ~$10
        "powders":      80.0,   # Vihtavuori tops out ~$70/lb
        "primers":       0.25,  # match-grade primers ~$0.12-0.18/ea
        "brass":        10.0,   # per case
        "factory_ammo":  3.0,   # per round
        "coatings":     50.0,   # per lb
    }
    _ceil = _CEIL.get(category, 0)
    if _ceil > 0:
        _ceiled = [o for o in in_stock if o.per_unit <= _ceil]
        if _ceiled:
            _dropped = len(in_stock) - len(_ceiled)
            if _dropped:
                log.debug(f"  Price ceiling ${_ceil}/unit dropped {_dropped} offer(s)")
            in_stock = _ceiled
        else:
            log.warning(f"  Price ceiling ${_ceil}/unit would drop ALL offers — keeping originals")

    best     = min(in_stock, key=lambda o: o.per_unit)
    snapshot = {
        "date":           TODAY,     "component_id":   comp_id,
        "component_name": comp_name, "category":       category,
        "best_per_unit":  best.per_unit, "best_price": best.price,
        "best_qty":       best.qty,  "best_unit":      best.unit,
        "best_vendor":    best.vendor, "best_url":     best.url,
        "offer_count":    len(offers), "last_updated": TODAY,
        "all_offers":     [asdict(o) for o in sorted(offers, key=lambda x: x.per_unit)],
        **trends,
    }
    if dry_run:
        log.info(f"  [DRY RUN] {comp_name}: ${best.per_unit:.4f}/{best.unit} "
                 f"@ {best.vendor} | alert={trends.get('alert')}")
        return best
    db.collection("prices").document(comp_id).collection("history").document(TODAY).set(snapshot)
    db.collection("prices").document(comp_id).set(snapshot)
    log.info(f"  ✓ {comp_name}: ${best.per_unit:.4f}/{best.unit} @ {best.vendor} "
             f"| alert={trends.get('alert')} | 7d={trends.get('trend_7d')}%")
    return best

# ── Email alerts ──────────────────────────────────────────────────
def send_alert_email(alerts):
    if not alerts:
        return
    frm = os.environ.get("ALERT_EMAIL_FROM")
    to  = os.environ.get("ALERT_EMAIL_TO")
    pwd = os.environ.get("ALERT_EMAIL_PASS")
    if not all([frm, to, pwd]):
        log.info("  Email alerts not configured (set ALERT_EMAIL_FROM / TO / PASS)")
        return

    buy_list   = [a for a in alerts if a["alert"] == "buy"]
    stock_list = [a for a in alerts if a["alert"] == "stock_up"]
    subject    = (f"🟢 AMMO IQ: {len(buy_list)} BUY signal(s) — {TODAY}" if buy_list
                  else f"⚠ AMMO IQ: {len(stock_list)} STOCK UP alert(s) — {TODAY}")

    def rows(items, trend_key, trend_label):
        html = ""
        for a in items:
            html += f"""<tr>
              <td style="padding:9px 14px;font-weight:600;">{a['name']}</td>
              <td style="padding:9px 14px;font-weight:700;">${a['per_unit']:.4f}/{a['unit']}</td>
              <td style="padding:9px 14px;">{a['vendor']}</td>
              <td style="padding:9px 14px;">{a[trend_key]:+.1f}% {trend_label}</td>
              <td style="padding:9px 14px;"><a href="{a['url']}">→ Buy</a></td>
            </tr>"""
        return html

    def table(title, color, items, trend_key, trend_label):
        if not items:
            return ""
        return f"""<h2 style="color:{color};margin-top:24px;">{title}</h2>
        <table style="width:100%;border-collapse:collapse;font-size:13px;">
          <thead><tr style="background:#f8f9fa;">
            <th style="text-align:left;padding:8px 14px;">Component</th>
            <th style="text-align:left;padding:8px 14px;">Best Price</th>
            <th style="text-align:left;padding:8px 14px;">Vendor</th>
            <th style="text-align:left;padding:8px 14px;">Trend</th>
            <th style="text-align:left;padding:8px 14px;">Link</th>
          </tr></thead>
          <tbody>{rows(items, trend_key, trend_label)}</tbody>
        </table>"""

    body = f"""<html><body style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto;">
      <div style="background:#2d4a52;padding:20px 28px;border-radius:10px 10px 0 0;">
        <h1 style="color:#fff;margin:0;font-size:24px;letter-spacing:2px;">AMMO IQ</h1>
        <p style="color:rgba(255,255,255,0.65);margin:4px 0 0;font-size:13px;">Daily Price Intelligence — {TODAY}</p>
      </div>
      <div style="border:1px solid #dde3e5;border-top:none;padding:24px;border-radius:0 0 10px 10px;">
        {table("🟢 Buy Now — Below 90-Day Average", "#27ae60", buy_list, "trend_7d", "7d")}
        {table("⚠ Stock Up — Rising Fast", "#c07828", stock_list, "trend_30d", "30d")}
        <p style="margin-top:24px;font-size:11px;color:#8fa8b0;">
          AMMO IQ — The Practical Pewologist · <a href="#">Open Dashboard</a>
        </p>
      </div>
    </body></html>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = frm
        msg["To"]      = to
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
    if args.verbose:
        log.setLevel(logging.DEBUG)

    log.info("=" * 60)
    log.info(f"Ammo Radar Scraper v2.7 — {TODAY}")
    if args.dry_run:
        log.info("*** DRY RUN — Firebase will NOT be written ***")
    log.info("=" * 60)

    with open(COMPONENTS_F) as f:
        config = yaml.safe_load(f)

    db = None
    try:
        db = init_firebase()
        log.info("Firebase connected ✓")
    except SystemExit:
        if not args.dry_run:
            raise
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
        if not components:
            continue
        log.info(f"\n── {category.upper()} ({len(components)}) ──")
        for comp in components:
            comp["_category"] = category   # inject for sanity checks in price_anchor_offers
            comp_id, comp_name = comp["id"], comp["name"]
            vendors = comp.get("vendors", ["powder_valley", "grafs", "midsouth"])
            log.info(f"Scraping: {comp_name}")
            all_offers = []
            for vk in vendors:
                fn = VENDOR_SCRAPERS.get(vk)
                if not fn:
                    log.warning(f"  No scraper registered for vendor '{vk}' — skipping")
                    continue
                try:
                    found = fn(comp)
                    # Apply title validation post-fetch for all vendors.
                    # eBay already validates inline; other scrapers benefit from
                    # the same title_require_any / title_reject / title_must_also_contain_any
                    # rules defined in components.yaml.
                    if found and comp.get("title_require_any") or comp.get("title_reject") or comp.get("title_must_also_contain_any"):
                        before = len(found)
                        found  = [o for o in found if validate_title(o.title or o.url, comp)]
                        after  = len(found)
                        if before != after:
                            log.debug(f"  {vk}: title filter dropped {before - after} offer(s)")
                    if found:
                        log.info(f"  {vk}: {len(found)} offer(s)")
                    all_offers.extend(found)
                except Exception as e:
                    log.warning(f"  {vk} error: {e}")

            if not all_offers:
                log.warning(f"  ✗ No data: {comp_name}")
                stats["no_data"] += 1
                continue

            # Drop offers that are unrealistically cheap (accessories, targets, etc.)
            floor = CATEGORY_MIN_PER_UNIT.get(category, 0)
            if floor > 0:
                floored = [o for o in all_offers if o.per_unit >= floor]
                if floored:
                    dropped = len(all_offers) - len(floored)
                    if dropped:
                        log.debug(f"  Price floor ${floor}/unit dropped {dropped} offer(s)")
                    all_offers = floored
                else:
                    log.warning(f"  Price floor ${floor}/unit would drop ALL offers — keeping originals")

            try:
                in_stock      = [o for o in all_offers if o.in_stock] or all_offers
                # Outlier filter: discard per_unit > 5x median of all offers
                raw_units = sorted(o.per_unit for o in in_stock)
                if len(raw_units) >= 3:
                    median = raw_units[len(raw_units)//2]
                    in_stock = [o for o in in_stock if o.per_unit <= median * 5]
                best_per_unit = min(o.per_unit for o in in_stock) if in_stock else None
                trends        = compute_trends(db, comp_id, best_per_unit)
                best          = write_to_firebase(
                    db, comp_id, comp_name, category,
                    all_offers, trends, args.dry_run,
                    component=comp,
                )
                if best and trends.get("alert") in ("buy", "stock_up") and not args.no_email:
                    alerts.append({
                        "name":      comp_name,
                        "per_unit":  best.per_unit,
                        "unit":      best.unit,
                        "vendor":    best.vendor,
                        "url":       best.url,
                        "alert":     trends["alert"],
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
    log.info(
        f"Done — ✓{stats['success']} written  ✗{stats['no_data']} no data  "
        f"⚠{stats['error']} errors  📬{len(alerts)} alerts"
    )
    if args.dry_run:
        log.info("DRY RUN complete — Firebase unchanged")
    log.info("=" * 60)


if __name__ == "__main__":
    run_scraper()
