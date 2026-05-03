#!/usr/bin/env python3
"""
AMMO IQ — Daily Price Harvester v2.3
Playwright + email alerts + dry-run mode.

Usage:
  python scraper.py                       # normal daily run
  python scraper.py --dry-run             # scrape, do NOT write to Firebase
  python scraper.py --component cci_sp    # single component
  python scraper.py --category primers    # single category
  python scraper.py --no-email            # suppress alert emails

Changelog:
  v2.3 — All vendor URLs and selectors confirmed via live diagnostic.
          Grafs: new URL /retail/catalog/search?keywords= + price-anchor strategy.
          Lucky Gunner: switched to Playwright + caliber category URL map.
          Rotometals: BigCommerce URL /search.php?search_query= + li.product selector.
          Midsouth: correct URL /search?SearchTerm= + .product card selector.
          Powder Valley: new domain powdervalley.com + Playwright + ais-Hits-item selector.
  v2.2 — Fixed Rotometals URL (removed &type=product). Added eBay scraper.
          Metals updated to [rotometals, ebay] only.
  v2.1 — Fixed Midsouth fragment URL. Switched Brownells to Playwright.
          Added rotometals, starline, magnus_bullets, bayou_bullets, harbor_freight.
          Hardened all selectors with fallback chains.
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

# Lucky Gunner uses category URLs per caliber, not a search endpoint.
LG_CALIBER_URLS = {
    "9mm":    "https://www.luckygunner.com/handgun/9mm-ammo",
    "45acp":  "https://www.luckygunner.com/handgun/45-acp-ammo",
    "38spl":  "https://www.luckygunner.com/handgun/38-special-ammo",
    "357mag": "https://www.luckygunner.com/handgun/357-magnum-ammo",
    "22lr":   "https://www.luckygunner.com/rimfire/22-lr-ammo",
}

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
            log.error("No Firebase credentials. Set FIREBASE_CREDENTIALS env var or add serviceAccount.json")
            sys.exit(1)
        cred = credentials.Certificate(cred_file)
    firebase_admin.initialize_app(cred, {
        "projectId": os.environ.get("FIREBASE_PROJECT_ID", "ammiq-pricing")
    })
    return firestore.client()

# ── Helpers ───────────────────────────────────────────────────────
_PRICE_RE = re.compile(r"\$?([\d,]+\.[\d]{2})")

def parse_price(text: str) -> Optional[float]:
    text = text.strip().replace(",", "")
    # Handle ranges like "$40.95 - $304.95" — take the lower value
    parts = text.split(" - ")
    m = _PRICE_RE.search(parts[0])
    return float(m.group(1).replace(",", "")) if m else None

def get_qty(component: dict, default: float = 1.0) -> float:
    try:
        return float(component.get("unit", default))
    except (ValueError, TypeError):
        return default

def first_el(card, selectors: list):
    for sel in selectors:
        el = card.select_one(sel)
        if el:
            return el
    return None

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

def price_anchor_offers(soup, vendor_name, component, link_domain,
                        price_selector=None, link_pattern=None, max_results=5):
    """
    Find products by locating price elements and walking up the DOM to find
    the nearest ancestor that contains a product link. Used for sites where
    the product card class names are non-standard (Grafs, Lucky Gunner).

    price_selector: CSS selector for price elements (e.g. 'span.price')
                    If None, finds <span> elements whose text looks like a price.
    link_pattern:   Regex pattern that product hrefs must match.
    """
    offers = []
    seen  = set()

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

        # Walk up DOM to find a container that has a product link
        container = price_el
        link_el   = None
        for _ in range(10):
            container = container.parent
            if not container or container.name in ("html", "body", "[document]"):
                break
            pattern = link_pattern or rf"{re.escape(link_domain)}"
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

        qty = get_qty(component)
        offers.append(PriceOffer(
            vendor_name, price, qty,
            str(component.get("unit", "1")),
            round(price / qty, 6) if qty else price,
            href,
        ))
        if len(offers) >= max_results:
            break

    return offers


# ── Vendor scrapers ───────────────────────────────────────────────

def scrape_powder_valley(component):
    """
    Powder Valley — domain changed to powdervalley.com, now JS-rendered.
    Uses Playwright. Products in li.ais-Hits-item.product (Algolia search widget).
    Price may be a range like "$40.95 - $304.95" — parse_price() takes the lower.
    """
    offers = []
    for term in component.get("search_terms", [])[:2]:
        url  = f"https://www.powdervalley.com/search/?q={requests.utils.quote(term)}"
        log.info(f"  Powder Valley (Playwright): {url[:70]}")
        soup = fetch_js(url, wait_selector="li.ais-Hits-item, li.product", wait_ms=4000)
        if not soup:
            continue
        cards = soup.select("li.ais-Hits-item.product, li.ais-Hits-item, li.product")
        for card in cards[:5]:
            price_el = first_el(card, [".price", ".amount", "span.price"])
            link_el  = card.select_one("a[href*='/product/'], a.prod-img-w, a[href]")
            if not price_el or not link_el:
                continue
            price = parse_price(price_el.get_text())
            if not price:
                continue
            qty  = get_qty(component)
            href = link_el["href"]
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
    Graf & Sons — confirmed URL: /retail/catalog/search?keywords=<term>
    Products are in a custom catalog layout with no standard product card class.
    Uses price-anchor strategy: finds <span> price elements and walks up DOM
    to locate the nearest product link container.
    Confirmed prices: $40.99 / $156.99 / $307.99 for Titegroup 1lb/5lb/8lb.
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
            link_pattern=r"grafs\.com/retail/catalog/product",
        )
        offers.extend(found)
    return offers


def scrape_midsouth(component):
    """
    Midsouth Shooters Supply — confirmed URL: /search?SearchTerm=<term>
    JS-rendered, uses Playwright. Products in div.product (3 results per search
    for multiple pack sizes). Price in first <span> with $ pattern inside card.
    Confirmed: Titegroup 1lb=$41.99, 5lb=$159.99, 8lb=$313.99.
    """
    offers = []
    for term in component.get("search_terms", [])[:2]:
        url  = f"https://www.midsouthshooterssupply.com/search?SearchTerm={requests.utils.quote(term)}"
        log.info(f"  Midsouth (Playwright): {url[:70]}")
        soup = fetch_js(url, wait_selector=".product-wrapper, .product", wait_ms=5000)
        if not soup:
            continue
        cards = soup.select(".product")
        for card in cards[:6]:
            # Price is in a bare <span> with no class — find first one matching $ pattern
            price_el = None
            for span in card.find_all("span"):
                text = span.get_text(strip=True)
                if _PRICE_RE.match(text) or text.startswith("$"):
                    price_el = span
                    break
            link_el = card.select_one("a[href*='/item/'], a[href]")
            if not price_el or not link_el:
                continue
            price = parse_price(price_el.get_text())
            if not price:
                continue
            qty  = get_qty(component)
            href = link_el["href"]
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
    Lucky Gunner — JS-rendered, requires Playwright.
    Does NOT have a search endpoint — uses caliber category pages (e.g. /handgun/9mm-ammo).
    Price elements confirmed as <span class='price'> and <span class='regular-price'>.
    Uses price-anchor strategy to find product containers.
    Only works for components with a 'caliber' field (factory ammo). Returns []
    for reloading components (powders, primers, brass) — remove lucky_gunner
    from those vendors lists in components.yaml.
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
    soup = fetch_js(url, wait_selector="span.price, span.regular-price", wait_ms=5000)
    if not soup:
        return []

    # Filter to the specific brand/product using search_terms
    search_keywords = []
    for term in component.get("search_terms", []):
        search_keywords.extend(term.lower().split())

    offers = price_anchor_offers(
        soup, "Lucky Gunner", component,
        link_domain="https://www.luckygunner.com",
        price_selector="span.price, span.regular-price",
        link_pattern=r"luckygunner\.com/",
        max_results=8,
    )

    # Filter offers to only those matching the tracked product's keywords
    brand = component.get("brand", "").lower()
    grain = str(component.get("grain", ""))
    filtered = []
    for offer in offers:
        # Try to find the product title near the offer URL
        link_el = soup.find("a", href=lambda h: h and offer.url in h)
        title = link_el.get_text(strip=True).lower() if link_el else ""
        if brand and brand not in title and not any(k in title for k in brand.split()):
            continue
        filtered.append(offer)

    return filtered if filtered else offers[:3]


def scrape_ammoseek(component):
    """AmmoSeek — JS-rendered aggregator, Playwright. Returns cost-per-round rows."""
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
    soup  = fetch_js(url, wait_selector="tr.offer-row, .listing-item", wait_ms=5000)
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
        soup = fetch_js(url, wait_selector=".product-item, .product-detail", wait_ms=4000)
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
        soup = fetch_js(url, wait_selector=".js-product-card, .product-item", wait_ms=5000)
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
    Rotometals — BigCommerce store (not Shopify as previously assumed).
    Confirmed URL: /search.php?search_query=<term>&section=product
    Confirmed card: li.product > article.card
    Confirmed price: span.price.price--withoutTax.price--main
    """
    offers = []
    for term in component.get("search_terms", [])[:2]:
        url  = f"https://www.rotometals.com/search.php?search_query={requests.utils.quote(term)}&section=product"
        soup = fetch_static(url)
        if not soup:
            continue
        cards = soup.select("li.product")
        for card in cards[:5]:
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
            qty  = get_qty(component)
            href = link_el["href"]
            if href.startswith("/"):
                href = "https://www.rotometals.com" + href
            offers.append(PriceOffer(
                "Rotometals", price, qty,
                str(component.get("unit", "lb")),
                round(price / qty, 6) if qty else price, href,
            ))
    return offers


def scrape_ebay(component):
    """
    eBay — Buy It Now listings sorted by price+shipping lowest first.
    LH_BIN=1 (Buy It Now only), _sop=15 (price+ship low→high), LH_ItemCondition=3 (New).
    Adds shipping cost to price when not free.
    """
    offers = []
    for term in component.get("search_terms", [])[:2]:
        url = (
            f"https://www.ebay.com/sch/i.html"
            f"?_nkw={requests.utils.quote(term)}"
            f"&LH_BIN=1&_sop=15&LH_ItemCondition=3"
        )
        soup = fetch_static(url)
        if not soup:
            continue
        for card in soup.select(".s-item")[:8]:
            title_el = card.select_one(".s-item__title")
            price_el = first_el(card, [".s-item__price", ".notranslate"])
            ship_el  = card.select_one(".s-item__shipping, .s-item__freeXDays")
            link_el  = card.select_one("a.s-item__link, a[href*='ebay.com/itm']")
            if title_el and "shop on ebay" in title_el.get_text().lower():
                continue
            if not price_el:
                continue
            price = parse_price(price_el.get_text().split(" to ")[0])
            if not price:
                continue
            if ship_el:
                ship_text = ship_el.get_text().lower()
                if "free" not in ship_text:
                    ship_cost = parse_price(ship_el.get_text())
                    if ship_cost:
                        price += ship_cost
            qty = get_qty(component)
            offers.append(PriceOffer(
                "eBay", price, qty,
                str(component.get("unit", "lb")),
                round(price / qty, 6) if qty else price,
                link_el["href"] if link_el else url,
            ))
    return offers


def scrape_starline(component):
    """Starline Brass — unprimed brass cases, static HTML."""
    offers = []
    for term in component.get("search_terms", [])[:2]:
        url  = f"https://www.starlinebrass.com/search/?q={requests.utils.quote(term)}"
        soup = fetch_static(url)
        if not soup:
            continue
        cards = soup.select(".product-card, .product-item, .product, li.product")
        for card in cards[:5]:
            price_el = first_el(card, [
                ".product-price", ".woocommerce-Price-amount", ".price bdi", ".price",
            ])
            link_el  = card.select_one("a[href*='/brass/'], a[href*='/products/'], a[href]")
            stock_el = card.select_one(".stock, .in-stock, .out-of-stock, .availability")
            if not price_el:
                continue
            price    = parse_price(price_el.get_text())
            if not price:
                continue
            in_stock = "out" not in (stock_el.get_text().lower() if stock_el else "in")
            qty      = get_qty(component, 500.0)
            href     = link_el["href"] if link_el else url
            if href.startswith("/"):
                href = "https://www.starlinebrass.com" + href
            offers.append(PriceOffer(
                "Starline", price, qty,
                str(component.get("unit", "500")),
                round(price / qty, 6) if qty else price, href, in_stock,
            ))
    return offers


def scrape_magnus_bullets(component):
    """Magnus Bullets — WooCommerce shop for Hi-Tek coated cast bullets."""
    offers = []
    for term in component.get("search_terms", [])[:2]:
        url  = f"https://www.magnusbullets.com/?s={requests.utils.quote(term)}&post_type=product"
        soup = fetch_static(url)
        if not soup:
            continue
        cards = soup.select("li.product, .product, .product-small")
        for card in cards[:5]:
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
    """Bayou Bullets — WooCommerce shop for Hi-Tek coated cast bullets."""
    offers = []
    for term in component.get("search_terms", [])[:2]:
        url  = f"https://bayoubullets.com/?s={requests.utils.quote(term)}&post_type=product"
        soup = fetch_static(url)
        if not soup:
            continue
        cards = soup.select("li.product, .product, .product-small")
        for card in cards[:5]:
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
    """Harbor Freight — powder coat supplies. May be blocked by Cloudflare."""
    offers = []
    for term in component.get("search_terms", [])[:1]:
        url  = f"https://www.harborfreight.com/catalogsearch/result/?q={requests.utils.quote(term)}"
        soup = fetch_static(url)
        if not soup:
            continue
        title_tag = soup.find("title")
        if title_tag and "just a moment" in title_tag.get_text().lower():
            log.warning("  Harbor Freight: Cloudflare challenge detected — scraper blocked.")
            return []
        cards = soup.select("[data-qa='product-card'], .grid-item, .product-card, .product-item")
        for card in cards[:5]:
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
    """Amazon — blocked by bot detection. Returns nothing."""
    log.warning(
        "  Amazon scraping is blocked. Remove 'amazon' from this component's "
        "vendors in components.yaml."
    )
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
        docs = ref.order_by("__name__", direction="DESCENDING").limit(90).stream()
        hist = [d.to_dict().get("best_per_unit") for d in docs if d.to_dict().get("best_per_unit")]
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
def write_to_firebase(db, comp_id, comp_name, category, offers, trends, dry_run=False):
    if not offers:
        log.warning(f"  No offers — skipping {comp_name}")
        return None
    in_stock = [o for o in offers if o.in_stock] or offers
    best     = min(in_stock, key=lambda o: o.per_unit)
    snapshot = {
        "date":           TODAY,     "component_id":   comp_id,
        "component_name": comp_name, "category":       category,
        "best_per_unit":  best.per_unit, "best_price": best.price,
        "best_qty":       best.qty,  "best_unit":      best.unit,
        "best_vendor":    best.vendor, "best_url":     best.url,
        "offer_count":    len(offers), "last_updated": TODAY,
        "all_offers":     [asdict(o) for o in sorted(offers, key=lambda x: x.per_unit)[:10]],
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
            s.ehlo()
            s.starttls()
            s.login(frm, pwd)
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
    log.info(f"AMMO IQ Scraper v2.3 — {TODAY}")
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
                    if found:
                        log.info(f"  {vk}: {len(found)} offer(s)")
                    all_offers.extend(found)
                except Exception as e:
                    log.warning(f"  {vk} error: {e}")

            if not all_offers:
                log.warning(f"  ✗ No data: {comp_name}")
                stats["no_data"] += 1
                continue

            try:
                in_stock      = [o for o in all_offers if o.in_stock] or all_offers
                best_per_unit = min(o.per_unit for o in in_stock)
                trends        = compute_trends(db, comp_id, best_per_unit)
                best          = write_to_firebase(
                    db, comp_id, comp_name, category,
                    all_offers, trends, args.dry_run,
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
