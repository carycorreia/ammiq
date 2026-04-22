#!/usr/bin/env python3
"""
AMMO IQ — Daily Price Harvester v2
Playwright + email alerts + dry-run mode.

Usage:
  python scraper.py                       # normal daily run
  python scraper.py --dry-run             # scrape, do NOT write to Firebase
  python scraper.py --component cci_sp    # single component
  python scraper.py --category primers    # single category
  python scraper.py --no-email            # suppress alert emails
"""

import os, sys, json, time, logging, datetime, argparse, smtplib, asyncio
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
def parse_price(text: str) -> Optional[float]:
    import re
    text = text.strip().replace(",", "")
    m = re.search(r"\$?([\d]+\.[\d]{1,2})", text)
    return float(m.group(1)) if m else None

def get_qty(component: dict, default: float = 1.0) -> float:
    try:
        return float(component.get("unit", default))
    except (ValueError, TypeError):
        return default

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
                try:
                    await page.wait_for_selector(wait_selector, timeout=8000)
                except Exception:
                    pass
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

def fetch_js(url: str, wait_selector: str = None, wait_ms: int = 3500) -> Optional[BeautifulSoup]:
    return asyncio.run(_fetch_js(url, wait_selector, wait_ms))

# ── Vendor scrapers ───────────────────────────────────────────────

def scrape_powder_valley(component):
    offers = []
    for term in component.get("search_terms", [])[:2]:
        url  = f"https://www.powdervalleyinc.com/search?q={requests.utils.quote(term)}"
        soup = fetch_static(url)
        if not soup: continue
        for card in soup.select(".product-item, .product-grid-item, [data-product-id]")[:5]:
            price_el = card.select_one(".price, .special-price .price")
            link_el  = card.select_one("a[href]")
            if not price_el: continue
            price = parse_price(price_el.get_text())
            if not price: continue
            qty = get_qty(component)
            href = link_el["href"] if link_el else url
            if href.startswith("/"): href = "https://www.powdervalleyinc.com" + href
            offers.append(PriceOffer("Powder Valley", price, qty,
                str(component.get("unit","1")), round(price/qty,6) if qty else price, href))
    return offers

def scrape_grafs(component):
    offers = []
    for term in component.get("search_terms", [])[:2]:
        url  = f"https://www.grafs.com/search?query={requests.utils.quote(term)}"
        soup = fetch_static(url)
        if not soup: continue
        for card in soup.select(".product-item, .item.product")[:5]:
            price_el = card.select_one(".price, .regular-price")
            link_el  = card.select_one("a.product-item-link, a[href*='/catalog/product']")
            if not price_el: continue
            price = parse_price(price_el.get_text())
            if not price: continue
            qty = get_qty(component)
            offers.append(PriceOffer("Grafs", price, qty,
                str(component.get("unit","1")), round(price/qty,6) if qty else price,
                link_el["href"] if link_el else url))
    return offers

def scrape_midsouth(component):
    offers = []
    for term in component.get("search_terms", [])[:2]:
        url  = f"https://www.midsouthshooterssupply.com/search#{requests.utils.quote(term)}"
        soup = fetch_static(url)
        if not soup: continue
        for card in soup.select(".product-container, .product-item, .ms-product-card")[:5]:
            price_el = card.select_one(".product-price, .price-box .price, .ms-price")
            link_el  = card.select_one("a[href]")
            if not price_el: continue
            price = parse_price(price_el.get_text())
            if not price: continue
            qty = get_qty(component)
            offers.append(PriceOffer("Midsouth", price, qty,
                str(component.get("unit","1")), round(price/qty,6) if qty else price,
                link_el["href"] if link_el else url))
    return offers

def scrape_lucky_gunner(component):
    offers = []
    for term in component.get("search_terms", [])[:1]:
        url  = f"https://www.luckygunner.com/search?q={requests.utils.quote(term)}"
        soup = fetch_static(url)
        if not soup: continue
        for card in soup.select(".product, .ammo-listing, .lg-product-card")[:5]:
            price_el = card.select_one(".price, .ammo-price, .lg-price")
            link_el  = card.select_one("a[href]")
            stock_el = card.select_one(".in-stock, .out-of-stock, .stock-status")
            if not price_el: continue
            price    = parse_price(price_el.get_text())
            if not price: continue
            in_stock = "out" not in (stock_el.get_text().lower() if stock_el else "in")
            qty      = get_qty(component, 50.0)
            offers.append(PriceOffer("Lucky Gunner", price, qty,
                str(component.get("unit","50")), round(price/qty,6) if qty else price,
                link_el["href"] if link_el else url, in_stock))
    return offers

def scrape_ammoseek(component):
    """JS-rendered — uses Playwright."""
    caliber = component.get("caliber", "")
    if not caliber: return []
    cal_map = {"9mm":"9mm-luger","45acp":"45-auto","38spl":"38-special",
               "357mag":"357-magnum","22lr":"22-long-rifle"}
    slug  = cal_map.get(caliber, caliber.replace(" ","-"))
    grain = component.get("grain","")
    url   = f"https://ammoseek.com/ammo/{slug}" + (f"?gr={grain}" if grain else "")
    log.info(f"  AmmoSeek (Playwright): {url}")
    soup  = fetch_js(url, wait_selector="tr.offer-row, .listing-item", wait_ms=4500)
    if not soup: return []
    offers = []
    for row in soup.select("tr.offer-row, .listing-item, .ammo-row")[:10]:
        price_el  = row.select_one(".price-per-round, .cpr, td.cpr, [data-cpr]")
        vendor_el = row.select_one(".retailer, .vendor-name, td.vendor, .seller")
        link_el   = row.select_one("a[href]")
        stock_el  = row.select_one(".stock, .availability")
        if not price_el: continue
        cpr = parse_price(price_el.get_text())
        if not cpr: continue
        box_qty  = get_qty(component, 50.0)
        in_stock = "out" not in (stock_el.get_text().lower() if stock_el else "in")
        offers.append(PriceOffer(
            vendor_el.get_text().strip() if vendor_el else "AmmoSeek",
            round(cpr*box_qty,4), box_qty, str(component.get("unit","50")),
            cpr, link_el["href"] if link_el else url, in_stock))
    return offers

def scrape_target_sports(component):
    """JS-rendered — uses Playwright."""
    offers = []
    for term in component.get("search_terms", [])[:1]:
        url  = f"https://www.targetsportsusa.com/search.aspx?q={requests.utils.quote(term)}"
        log.info(f"  Target Sports (Playwright): {url[:60]}")
        soup = fetch_js(url, wait_selector=".product-item, .product-detail", wait_ms=3500)
        if not soup: continue
        for card in soup.select(".product-item, .product-detail, .ts-product-card")[:5]:
            price_el = card.select_one(".our-price, .price, .sale-price, [itemprop='price']")
            link_el  = card.select_one("a[href]")
            if not price_el: continue
            price = parse_price(price_el.get_text())
            if not price: continue
            qty = get_qty(component, 50.0)
            offers.append(PriceOffer("Target Sports USA", price, qty,
                str(component.get("unit","50")), round(price/qty,6) if qty else price,
                link_el["href"] if link_el else url))
    return offers

def scrape_brownells(component):
    offers = []
    for term in component.get("search_terms", [])[:1]:
        url  = f"https://www.brownells.com/search/index.htm?k={requests.utils.quote(term)}"
        soup = fetch_static(url)
        if not soup: continue
        for card in soup.select(".js-product-card, .product-item, [data-product]")[:5]:
            price_el = card.select_one(".price, .js-price, [itemprop='price']")
            link_el  = card.select_one("a[href]")
            if not price_el: continue
            price = parse_price(price_el.get_text())
            if not price: continue
            qty = get_qty(component)
            offers.append(PriceOffer("Brownells", price, qty,
                str(component.get("unit","1")), round(price/qty,6) if qty else price,
                link_el["href"] if link_el else url))
    return offers

VENDOR_SCRAPERS = {
    "powder_valley": scrape_powder_valley,
    "grafs":         scrape_grafs,
    "midsouth":      scrape_midsouth,
    "lucky_gunner":  scrape_lucky_gunner,
    "ammoseek":      scrape_ammoseek,
    "target_sports": scrape_target_sports,
    "brownells":     scrape_brownells,
}

# ── Trends ────────────────────────────────────────────────────────
def compute_trends(db, component_id, current_best):
    trends = {"trend_7d": None, "trend_30d": None, "alert": "hold", "avg_90d": None}
    if not db: return trends
    try:
        ref = db.collection("prices").document(component_id).collection("history")
        for days, key in [(7,"trend_7d"),(30,"trend_30d")]:
            past = (datetime.date.today()-datetime.timedelta(days=days)).isoformat()
            snap = ref.document(past).get()
            if snap.exists:
                old = snap.to_dict().get("best_per_unit")
                if old and old > 0:
                    trends[key] = round(((current_best-old)/old)*100, 1)
        docs = ref.order_by("__name__", direction="DESCENDING").limit(90).stream()
        hist = [d.to_dict().get("best_per_unit") for d in docs if d.to_dict().get("best_per_unit")]
        if hist:
            avg = sum(hist)/len(hist)
            trends["avg_90d"] = round(avg, 6)
            if len(hist) >= 14:
                pct = ((current_best-avg)/avg)*100
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
        "date":           TODAY,   "component_id":   comp_id,
        "component_name": comp_name, "category":     category,
        "best_per_unit":  best.per_unit, "best_price": best.price,
        "best_qty":       best.qty,  "best_unit":    best.unit,
        "best_vendor":    best.vendor, "best_url":   best.url,
        "offer_count":    len(offers), "last_updated": TODAY,
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
    frm  = os.environ.get("ALERT_EMAIL_FROM")
    to   = os.environ.get("ALERT_EMAIL_TO")
    pwd  = os.environ.get("ALERT_EMAIL_PASS")
    if not all([frm, to, pwd]):
        log.info("  Email alerts not configured (set ALERT_EMAIL_FROM / TO / PASS)")
        return

    buy_list    = [a for a in alerts if a["alert"] == "buy"]
    stock_list  = [a for a in alerts if a["alert"] == "stock_up"]
    subject     = f"🟢 AMMO IQ: {len(buy_list)} BUY signal(s) — {TODAY}" if buy_list \
                  else f"⚠ AMMO IQ: {len(stock_list)} STOCK UP alert(s) — {TODAY}"

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
        if not items: return ""
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
    if args.verbose: log.setLevel(logging.DEBUG)

    log.info("=" * 60)
    log.info(f"AMMO IQ Scraper v2 — {TODAY}")
    if args.dry_run: log.info("*** DRY RUN — Firebase will NOT be written ***")
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
        if not components: continue
        log.info(f"\n── {category.upper()} ({len(components)}) ──")
        for comp in components:
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
                in_stock      = [o for o in all_offers if o.in_stock] or all_offers
                best_per_unit = min(o.per_unit for o in in_stock)
                trends        = compute_trends(db, comp_id, best_per_unit)
                best          = write_to_firebase(db, comp_id, comp_name, category,
                                                  all_offers, trends, args.dry_run)
                if best and trends.get("alert") in ("buy","stock_up") and not args.no_email:
                    alerts.append({
                        "name": comp_name, "per_unit": best.per_unit,
                        "unit": best.unit,  "vendor":   best.vendor,
                        "url":  best.url,   "alert":    trends["alert"],
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
