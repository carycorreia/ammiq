# AMMO IQ — Scraper Repair Prompt for Claude Code

Use this file when a vendor scraper is broken. Run:
```bash
claude fix_scraper.md
```

---

## Context

You are maintaining `scraper.py` — a daily price harvester for the AMMO IQ reloading
intelligence platform. The scraper pulls prices from these vendors:

| Vendor | Method | Notes |
|---|---|---|
| Powder Valley | requests + BeautifulSoup | Static HTML |
| Grafs | requests + BeautifulSoup | Static HTML |
| Midsouth | requests + BeautifulSoup | Static HTML |
| Lucky Gunner | requests + BeautifulSoup | Static HTML |
| AmmoSeek | Playwright | JS-rendered — wait for `tr.offer-row` |
| Target Sports USA | Playwright | JS-rendered — wait for `.product-item` |
| Brownells | requests + BeautifulSoup | Static HTML |

---

## How to diagnose a broken scraper

### Step 1 — Run dry-run to see what's failing
```bash
cd scraper
python scraper.py --dry-run --verbose 2>&1 | tee debug.log
```

### Step 2 — Run a single vendor to isolate the problem
```bash
python scraper.py --dry-run --component titegroup
python scraper.py --dry-run --category powders
```

### Step 3 — Inspect the live page structure
Write a quick test script:
```python
from scraper import fetch_static, fetch_js
soup = fetch_static("https://www.powdervalleyinc.com/search?q=titegroup")
# or for JS pages:
soup = fetch_js("https://ammoseek.com/ammo/9mm-luger", wait_selector="tr.offer-row")
print(soup.prettify()[:3000])
```

Then look at what CSS selectors are actually present on the page and update
the relevant `scrape_*` function in `scraper.py`.

### Step 4 — Common fixes

**Problem: Vendor returns no results**
- Check if the site requires cookies or has bot detection
- Try rotating the User-Agent in HEADERS
- Add a longer delay: increase `DELAY` constant or add `time.sleep(5)` before fetch

**Problem: Prices extracted incorrectly**
- Print `price_el.get_text()` to see raw text
- Update the CSS selector in the relevant `scrape_*` function
- Check if price is in a data attribute: `card.select_one("[data-price]")["data-price"]`

**Problem: Playwright timeout on AmmoSeek / Target Sports**
- Increase `wait_ms` from 4500 to 6000
- Try a different `wait_selector` — inspect the page source for stable selectors
- Check if the site added Cloudflare protection — may need `stealth` mode:
  ```python
  from playwright_stealth import stealth_async
  await stealth_async(page)
  ```
  Then add `playwright-stealth` to requirements.txt

**Problem: Firebase write failing**
- Check credentials: `echo $FIREBASE_CREDENTIALS | python -c "import sys,json; print(json.load(sys.stdin).keys())"`
- Verify project ID: `echo $FIREBASE_PROJECT_ID`

---

## Adding a new vendor

1. Add a new `scrape_vendorname(component)` function following the pattern of existing scrapers
2. Register it in `VENDOR_SCRAPERS` dict
3. Add `vendorname` to the relevant component entries in `components.yaml`
4. Test: `python scraper.py --dry-run --component titegroup`

## Adding a new component

Edit `components.yaml` — no code changes needed. Add:
```yaml
powders:
  - id: cfe_pistol
    name: "CFE Pistol"
    brand: Hodgdon
    unit: lb
    vendors: [powder_valley, grafs, midsouth]
    search_terms:
      - "Hodgdon CFE Pistol 1lb powder"
```

Then seed it: `python scraper.py --dry-run --component cfe_pistol`

---

## Environment variables required

| Variable | Purpose | Where to set |
|---|---|---|
| `FIREBASE_CREDENTIALS` | Firebase service account JSON | GitHub Secret |
| `FIREBASE_PROJECT_ID` | Firebase project ID | GitHub Secret |
| `ALERT_EMAIL_FROM` | Alert sender email | GitHub Secret |
| `ALERT_EMAIL_TO` | Alert recipient email | GitHub Secret |
| `ALERT_EMAIL_PASS` | Sender email app password | GitHub Secret |

For Gmail: use an App Password (not your main password).
Go to Google Account → Security → 2-Step Verification → App passwords.

---

## Architecture reminder

```
components.yaml → scraper.py → Firebase Firestore
                                      ↓
                             index.html (AMMO IQ UI)
                             reads on page load via Firebase SDK
```

Firebase structure:
```
/prices/{component_id}          ← current best price (overwritten daily)
/prices/{component_id}/history/{YYYY-MM-DD}  ← daily snapshot (append-only)
```
