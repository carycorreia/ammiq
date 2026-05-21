#!/usr/bin/env python3
"""
clear_stale_prices.py — Remove obviously wrong price data from Firebase.

Deletes any factory_ammo document where best_per_unit > $2.00/round
(e.g. the $4.66/rd Blazer Brass entries from the case-price bug).
After running, the UI will show "—" instead of wrong prices until the
next scrape overwrites with correct data.

Usage:
    cd ~/Downloads/ammiq/scraper
    python clear_stale_prices.py            # preview only (dry run)
    python clear_stale_prices.py --delete   # actually delete
"""

import sys, os, json
sys.path.insert(0, os.path.dirname(__file__))

import firebase_admin
from firebase_admin import credentials, firestore

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def init_firebase():
    if firebase_admin._apps:
        return firestore.client()
    cred_json = os.environ.get("FIREBASE_CREDENTIALS")
    if cred_json:
        cred = credentials.Certificate(json.loads(cred_json))
    else:
        cred_file = os.path.join(SCRIPT_DIR, "serviceAccount.json")
        cred = credentials.Certificate(cred_file)
    firebase_admin.initialize_app(cred, {"projectId": "ammiq-d63b2"})
    return firestore.client()

# Price sanity limits — anything outside these is stale/wrong
SANITY = {
    "factory_ammo": (0.01, 2.00),
    "primers":      (0.01, 0.30),
    "brass":        (0.01, 2.00),
}

def main():
    delete_mode = "--delete" in sys.argv
    print(f"Mode: {'DELETE' if delete_mode else 'DRY RUN (preview only)'}")
    print("Connecting to Firebase...")
    db = init_firebase()

    docs = db.collection("prices").stream()
    stale = []

    for doc in docs:
        d = doc.to_dict()
        cat        = d.get("category", "")
        per_unit   = d.get("best_per_unit")
        name       = d.get("component_name", doc.id)
        vendor     = d.get("best_vendor", "?")
        price      = d.get("best_price", 0)
        qty        = d.get("best_qty", 0)
        updated    = d.get("last_updated", "?")

        if per_unit is None:
            continue

        lo, hi = SANITY.get(cat, (0, 1e9))
        if per_unit < lo or per_unit > hi:
            stale.append((doc.id, name, cat, per_unit, price, qty, vendor, updated))

    if not stale:
        print("\n✓ No stale entries found — all prices within sanity limits.")
        return

    print(f"\nFound {len(stale)} stale entries:\n")
    print(f"  {'ID':<25} {'Name':<35} {'$/unit':>8}  {'Price':>8} {'Qty':>6}  {'Vendor':<20} {'Updated'}")
    print("  " + "-"*115)
    for doc_id, name, cat, pu, price, qty, vendor, updated in stale:
        flag = "DELETE" if delete_mode else "WOULD DELETE"
        print(f"  {doc_id:<25} {name:<35} ${pu:>7.4f}  ${price:>7.2f} {qty:>6}  {vendor:<20} {updated}  ← {flag}")

    if delete_mode:
        print(f"\nDeleting {len(stale)} documents...")
        for doc_id, *_ in stale:
            db.collection("prices").document(doc_id).delete()
            print(f"  Deleted: {doc_id}")
        print(f"\n✓ Done. The UI will show '—' for these components until the next scrape.")
    else:
        print(f"\nRun with --delete to actually remove these entries.")

if __name__ == "__main__":
    main()
