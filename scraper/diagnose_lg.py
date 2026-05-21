#!/usr/bin/env python3
"""
diagnose_lg.py — Dump Lucky Gunner page structure so we can fix the scraper.

Usage:
    python diagnose_lg.py            # dumps 9mm page
    python diagnose_lg.py 22lr       # dumps 22LR page

Shows:
  - All price elements found and their text
  - The DOM path from each price element up to the root
  - All text containing "per round", "¢", "cents"
  - A short excerpt of raw page HTML
"""

import sys, re, asyncio
from bs4 import BeautifulSoup

LG_CALIBER_URLS = {
    "9mm":    "https://www.luckygunner.com/handgun/9mm-ammo",
    "45acp":  "https://www.luckygunner.com/handgun/45-acp-ammo",
    "38spl":  "https://www.luckygunner.com/handgun/38-special-ammo",
    "357mag": "https://www.luckygunner.com/handgun/357-magnum-ammo",
    "22lr":   "https://www.luckygunner.com/rimfire/22-lr-ammo",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

async def fetch(url):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx  = await browser.new_context(user_agent=HEADERS["User-Agent"])
        page = await ctx.new_page()
        print(f"Fetching {url} ...")
        await page.goto(url, timeout=30000)
        try:
            await page.wait_for_selector("span.price, span.regular-price, [class*='price']", timeout=10000)
        except Exception:
            print("  (selector timeout — continuing anyway)")
        await page.wait_for_timeout(3500)
        html = await page.content()
        await browser.close()
        return html

def diagnose(html):
    soup = BeautifulSoup(html, "html.parser")

    # ── 1. All price-like elements ────────────────────────────────────────
    print("\n" + "="*60)
    print("PRICE ELEMENTS FOUND")
    print("="*60)
    for sel in ["span.price", "span.regular-price", "[class*='price']", "[itemprop='price']"]:
        els = soup.select(sel)
        print(f"\n  {sel!r}: {len(els)} found")
        for el in els[:5]:
            txt = el.get_text(strip=True)
            if txt and any(c.isdigit() for c in txt):
                classes = el.get("class", [])
                print(f"    text={txt!r:30s}  class={classes}")

    # ── 2. Search all text for "per round", "¢", "cents" ─────────────────
    print("\n" + "="*60)
    print("TEXT CONTAINING 'PER ROUND' / '¢' / 'CENTS'")
    print("="*60)
    cpr_pattern = re.compile(r'(?:per\s*round|¢|cents?\s*(?:per|each))', re.IGNORECASE)
    for el in soup.find_all(string=cpr_pattern):
        txt = el.strip()
        if txt:
            parent = el.parent
            classes = parent.get("class", []) if parent else []
            tag = parent.name if parent else "?"
            print(f"  <{tag} class={classes}>: {txt!r}")

    # ── 3. DOM path for first span.price ─────────────────────────────────
    print("\n" + "="*60)
    print("DOM ANCESTRY OF FIRST span.price")
    print("="*60)
    first_price = soup.select_one("span.price, span.regular-price")
    if first_price:
        node = first_price
        depth = 0
        while node and node.name not in ("html", "[document]") and depth < 15:
            classes = node.get("class", [])
            txt_preview = node.get_text(" ", strip=True)[:60]
            links = node.find_all("a", href=True)
            link_hrefs = [a["href"][:50] for a in links[:2]]
            print(f"  {'  '*depth}<{node.name} class={classes}>")
            if link_hrefs:
                print(f"  {'  '*depth}  links: {link_hrefs}")
            if txt_preview:
                print(f"  {'  '*depth}  text:  {txt_preview!r}")
            node = node.parent
            depth += 1
    else:
        print("  NO span.price FOUND — dumping all class names with 'price' in them:")
        for el in soup.find_all(class_=re.compile(r'price', re.I)):
            print(f"  <{el.name} class={el.get('class',[])}> text={el.get_text(strip=True)[:40]!r}")

    # ── 4. All links on the page (first 20) ──────────────────────────────
    print("\n" + "="*60)
    print("SAMPLE PRODUCT LINKS")
    print("="*60)
    links = soup.find_all("a", href=True)
    product_links = [a for a in links if "/ammo" in a["href"] or
                     (a["href"].startswith("/") and len(a["href"]) > 10)]
    for a in product_links[:15]:
        print(f"  href={a['href'][:70]!r}  text={a.get_text(strip=True)[:40]!r}")

    # ── 5. Raw HTML snippet ───────────────────────────────────────────────
    print("\n" + "="*60)
    print("RAW HTML AROUND FIRST PRICE (500 chars)")
    print("="*60)
    if first_price:
        raw = str(first_price.parent.parent)[:500]
        print(raw)
    else:
        print(html[5000:5500])  # middle of page


if __name__ == "__main__":
    caliber = sys.argv[1] if len(sys.argv) > 1 else "9mm"
    url = LG_CALIBER_URLS.get(caliber)
    if not url:
        print(f"Unknown caliber '{caliber}'. Options: {list(LG_CALIBER_URLS)}")
        sys.exit(1)
    html = asyncio.run(fetch(url))
    print(f"\nPage fetched: {len(html):,} chars")
    diagnose(html)
    # Save raw HTML for inspection
    out = f"lg_{caliber}_page.html"
    with open(out, "w") as f:
        f.write(html)
    print(f"\nFull HTML saved to: {out}")
