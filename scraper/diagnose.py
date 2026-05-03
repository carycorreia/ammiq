#!/usr/bin/env python3
"""
AMMO IQ — Targeted Selector Diagnostic (Round 2)
Now using the confirmed correct URLs for each vendor.

Run from your scraper/ folder with venv active:
    python diagnose2.py
"""

import requests, asyncio
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SEP = "=" * 60

def fetch(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        print(f"  Status: {r.status_code}  Final URL: {r.url}")
        return BeautifulSoup(r.text, "html.parser") if r.ok else None
    except Exception as e:
        print(f"  ERROR: {e}")
        return None

async def fetch_js_async(url, wait_sel=None, wait_ms=5000):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=HEADERS["User-Agent"])
        page = await ctx.new_page()
        await page.goto(url, timeout=30000)
        if wait_sel:
            try:
                await page.wait_for_selector(wait_sel, timeout=10000)
                print(f"  Wait selector '{wait_sel}' found ✓")
            except:
                print(f"  Wait selector '{wait_sel}' NOT found — fell through to timeout")
                await page.wait_for_timeout(wait_ms)
        else:
            await page.wait_for_timeout(wait_ms)
        html = await page.content()
        await browser.close()
        return BeautifulSoup(html, "html.parser")

def fetch_js(url, wait_sel=None, wait_ms=5000):
    return asyncio.run(fetch_js_async(url, wait_sel, wait_ms))

def probe_selectors(soup, selectors, label):
    print(f"\n  --- Selector probe: {label} ---")
    for sel in selectors:
        els = soup.select(sel)
        if els:
            first = str(els[0])[:400]
            print(f"  ✓ '{sel}' → {len(els)} results")
            print(f"     First: {first}")
        else:
            print(f"  ✗ '{sel}' → 0 results")

def show_prices(soup, label):
    import re
    price_re = re.compile(r'\$\s*[\d]+\.[\d]{2}')
    found = []
    for el in soup.find_all(string=price_re):
        tag = el.parent
        classes = " ".join(tag.get("class", []))
        found.append(f"    <{tag.name} class='{classes}'> text='{el.strip()[:60]}'")
    print(f"\n  --- Price elements in {label} ---")
    if found:
        for f in found[:15]:
            print(f)
    else:
        print("  (none found — page may still be JS-loading)")


# ── 1. GRAFS — confirmed URL ──────────────────────────────────────
print(SEP)
print("1. GRAFS  /retail/catalog/search?keywords=titegroup")
print(SEP)
url = "https://www.grafs.com/retail/catalog/search?keywords=titegroup"
soup = fetch(url)
if soup:
    print(f"  Page title: {soup.title.get_text().strip()[:80] if soup.title else 'n/a'}")
    probe_selectors(soup, [
        ".product-item", ".item", "li.item", ".catalog-product",
        ".product", "li.product", ".product-listing-item",
        "[class*='product']", "article",
    ], "grafs cards")
    show_prices(soup, "grafs")
    # Also dump top of body text so we can see page structure
    txt = soup.get_text(separator=" ", strip=True)
    print(f"\n  Body text (first 500 chars): {txt[:500]}")


# ── 2. LUCKY GUNNER — category page ──────────────────────────────
print()
print(SEP)
print("2. LUCKY GUNNER  /handgun/9mm-ammo")
print(SEP)
url = "https://www.luckygunner.com/handgun/9mm-ammo"
soup = fetch(url)
if soup:
    print(f"  Page title: {soup.title.get_text().strip()[:80] if soup.title else 'n/a'}")
    probe_selectors(soup, [
        ".ammo-table tbody tr", "tr.product", ".listing", ".product",
        ".ammo-listing", ".lg-product", ".result", "table tr",
        "[class*='ammo']", "[class*='product']", "[class*='listing']",
    ], "luckygunner cards")
    show_prices(soup, "luckygunner")
    txt = soup.get_text(separator=" ", strip=True)
    print(f"\n  Body text (first 500 chars): {txt[:500]}")


# ── 3. ROTOMETALS — BigCommerce search ───────────────────────────
print()
print(SEP)
print("3. ROTOMETALS  /search.php?search_query=raw+lead&section=product")
print(SEP)
url = "https://www.rotometals.com/search.php?search_query=raw+lead&section=product"
soup = fetch(url)
if soup:
    print(f"  Page title: {soup.title.get_text().strip()[:80] if soup.title else 'n/a'}")
    probe_selectors(soup, [
        "article.product", "li.product", ".productGrid-item",
        ".product-item", ".listing-item", "[class*='product']",
    ], "rotometals cards")
    show_prices(soup, "rotometals")
    txt = soup.get_text(separator=" ", strip=True)
    print(f"\n  Body text (first 500 chars): {txt[:500]}")


# ── 4. MIDSOUTH — correct URL, Playwright ────────────────────────
print()
print(SEP)
print("4. MIDSOUTH  /search?SearchTerm=titegroup  (Playwright)")
print(SEP)
url = "https://www.midsouthshooterssupply.com/search?SearchTerm=titegroup"
print("  Fetching with Playwright (10s wait)...")
try:
    soup = fetch_js(url, wait_sel=".product-container, .product-item, .product-card, .product", wait_ms=6000)
    print(f"  Page title: {soup.title.get_text().strip()[:80] if soup.title else 'n/a'}")
    probe_selectors(soup, [
        ".product-container", ".product-card", ".product-item",
        ".product", "li.product", "[class*='product']",
        ".item", ".search-result", ".result-item",
    ], "midsouth search results")
    show_prices(soup, "midsouth")
    txt = soup.get_text(separator=" ", strip=True)
    print(f"\n  Body text (first 600 chars): {txt[:600]}")
except Exception as e:
    print(f"  Playwright failed: {e}")


# ── 5. POWDER VALLEY — new domain, Playwright ────────────────────
print()
print(SEP)
print("5. POWDER VALLEY  powdervalley.com  (Playwright — new domain)")
print(SEP)
url = "https://www.powdervalley.com/search/?q=Hodgdon+Titegroup"
print("  Fetching with Playwright (8s wait)...")
try:
    soup = fetch_js(url, wait_sel=".product-item, .grid__item, .product", wait_ms=6000)
    print(f"  Page title: {soup.title.get_text().strip()[:80] if soup.title else 'n/a'}")
    probe_selectors(soup, [
        ".product-item", ".grid__item", ".product-card",
        "li.product", "[data-product-id]", ".product",
        ".collection-product", "[class*='product']",
    ], "powder valley cards")
    show_prices(soup, "powder valley")
    txt = soup.get_text(separator=" ", strip=True)
    print(f"\n  Body text (first 500 chars): {txt[:500]}")
except Exception as e:
    print(f"  Playwright failed: {e}")


print()
print(SEP)
print("Diagnostic 2 complete. Paste output back to Claude.")
print(SEP)
