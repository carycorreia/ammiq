# AMMO IQ — Reloading Intelligence Platform
### by The Practical Pewologist

A standalone price intelligence app that tracks reloading component and factory ammunition prices daily, computes trends, and tells you when to buy.

---

## Architecture

```
ammiq/
├── index.html              ← The web UI (deploy to Vercel)
├── scraper/
│   ├── scraper.py          ← Daily price harvester
│   ├── components.yaml     ← What to track (fully editable)
│   ├── requirements.txt    ← Python dependencies
│   └── serviceAccount.json ← Firebase credentials (local dev only, never commit)
└── .github/
    └── workflows/
        └── daily_scrape.yml ← GitHub Actions cron (runs free daily)
```

---

## Setup Guide

### Step 1 — Create a new Firebase project

1. Go to [console.firebase.google.com](https://console.firebase.google.com)
2. Click **Add project** → Name it `ammiq-pricing` → Create
3. Go to **Firestore Database** → Create database → Start in **test mode**
4. Go to **Project Settings** → **Service accounts** → **Generate new private key**
5. Save the downloaded JSON as `scraper/serviceAccount.json` (local dev)
6. Copy your **Project ID** (you'll need it for the UI config)

### Step 2 — Set up the Python scraper locally

```bash
cd scraper
pip install -r requirements.txt

# Test run (scrapes and writes to Firebase)
python scraper.py
```

### Step 3 — Set up GitHub Actions for daily scraping (free)

1. Create a new GitHub repo: `ammiq`
2. Push this code to it
3. Go to your repo → **Settings** → **Secrets and variables** → **Actions**
4. Add these secrets:
   - `FIREBASE_CREDENTIALS` — paste the entire contents of your `serviceAccount.json`
   - `FIREBASE_PROJECT_ID` — your Firebase project ID (e.g. `ammiq-pricing`)
5. GitHub will now run the scraper every day at 6am ET automatically — for free

### Step 4 — Deploy the UI to Vercel

1. Connect your `ammiq` GitHub repo to Vercel
2. Deploy — Vercel will serve `index.html` automatically
3. Open the app → go to **Settings** tab
4. Enter your Firebase Project ID and API Key
5. Click **Save Config** and reload

### Step 5 — Get your Firebase Web API Key

1. Firebase Console → Project Settings → General
2. Under **Your apps**, click **Add app** → **Web** → Register
3. Copy the `apiKey` value — enter it in AMMO IQ Settings

---

## Adding Components

Edit `scraper/components.yaml` to add any component. Example:

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

The scraper will pick it up on the next run. To test immediately:
```bash
cd scraper && python scraper.py
```

---

## Connecting to FIRST Shot Log

In the **Performance** tab, enter your FIRST app's Firebase Project ID.
AMMO IQ will read your shooting sessions and rank ammo by:
- Average score
- Average group size
- Sessions logged
- Value score (accuracy per dollar)

---

## Manual Scraper Trigger

From GitHub Actions UI:
1. Go to your repo → **Actions** → **AMMO IQ Daily Price Scraper**
2. Click **Run workflow** → **Run workflow**

---

## Vendor Coverage

| Vendor | Components | Factory Ammo |
|---|---|---|
| Powder Valley | ✅ Powders, Primers, Brass | ❌ |
| Graf & Sons | ✅ Powders, Primers, Brass | ❌ |
| Midsouth | ✅ Powders, Primers, Brass | ❌ |
| Lucky Gunner | ❌ | ✅ All calibers |
| Target Sports USA | ❌ | ✅ All calibers |
| AmmoSeek | ❌ | ✅ Aggregator (best for ammo) |

---

## Price Alert Logic

| Alert | Trigger |
|---|---|
| 🟢 BUY NOW | Current price ≥ 5% below 90-day average |
| ⚠ STOCK UP | 30-day trend ≥ +10% (rising fast) |
| HOLD | All other conditions |

Requires at least 14 days of price history to activate alerts.

---

## Brand
**AMMO IQ** by The Practical Pewologist
