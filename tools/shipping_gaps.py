"""Which carrier quotes are still missing, exactly.

    python3 tools/shipping_gaps.py                 summary under every zone policy
    python3 tools/shipping_gaps.py --policy full   the full zone-by-weight grid
    python3 tools/shipping_gaps.py --policy flat   just the weight ladder
    python3 tools/shipping_gaps.py --reachable     only weights the catalogue can actually produce

Reads shipping_rates.json — the verified quotes — and reports the cells with nothing in them.
It never fills a cell, never interpolates between two quotes, and never reuses a price from one
zone in another. A quote for 8 oz to one ZIP says nothing about 3 lb, and nothing about a
different zone; treating it as if it did is how a shop ends up eating postage on every distant
order.
"""
import json
import os
import sys
from collections import defaultdict

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RATES = os.path.join(APP_DIR, "shipping_rates.json")
PRODUCTS = os.path.join(APP_DIR, "products.json")

# The ladder the site prices on: everything under a pound is one flat band, then whole pounds.
BANDS = ["sub"] + [str(lb) for lb in range(1, 17)]
BAND_LABEL = {"sub": "under 1 lb"}

# USPS zones. 1-8 are distance bands from the origin; 9 covers offshore (AK, HI, territories).
ZONES = [str(z) for z in range(1, 10)]
BANDED = {"near (1-4)": ["1", "2", "3", "4"], "mid (5-6)": ["5", "6"], "far (7-9)": ["7", "8", "9"]}


def band_label(b):
    return BAND_LABEL.get(b, f"{b} lb")


def load_rates():
    with open(RATES, encoding="utf-8") as fh:
        return json.load(fh)


def reachable_bands():
    """Weights the catalogue can actually produce, so effort goes where orders really land."""
    import types
    path = os.path.join(APP_DIR, "db", "orders.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    mod = types.ModuleType("_o")
    mod.__file__ = path
    exec(compile(src, path, "exec"), mod.__dict__)
    W, pack = mod.CATEGORY_WEIGHT_OZ, mod.PACKAGING_OZ

    with open(PRODUCTS, encoding="utf-8") as fh:
        doc = json.load(fh)
    stock = defaultdict(int)
    for p in doc["products"]:
        stock[p["category"]] += sum(s["qty"] for s in p["sizes"])

    hits = {}
    for cat, units in stock.items():
        if not units or cat not in W:
            continue
        for n in range(1, min(units, 8) + 1):
            oz = W[cat] * n + pack
            band = "sub" if oz <= 15.99 else str(min(16, -(-int(oz) // 16)))
            hits.setdefault(band, []).append(f"{n}x {cat.lower()}")
    return hits


def verified_cells(rates):
    """(band, zone) -> quote. A quote with no zone cannot fill a cell in a zoned policy."""
    cells, unzoned = {}, []
    for q in rates.get("quotes", []):
        band = str(q.get("band"))
        zone = q.get("zone")
        if zone is None:
            unzoned.append(q)
        else:
            cells[(band, str(zone))] = q
    return cells, unzoned


def main():
    args = sys.argv[1:]
    policy = None
    if "--policy" in args:
        policy = args[args.index("--policy") + 1]
    only_reachable = "--reachable" in args

    rates = load_rates()
    cells, unzoned = verified_cells(rates)
    origin = rates.get("origin_zip")
    reach = reachable_bands()
    bands = [b for b in BANDS if b in reach] if only_reachable else BANDS

    print("Shipping quotes — what is verified and what is missing")
    print("=" * 70)
    print(f"carrier     : {rates.get('carrier')}")
    print(f"origin ZIP  : {origin or 'NOT SET — no destination can be mapped to a zone without it'}")
    print(f"zone policy : {rates.get('zone_policy')}")
    print(f"quotes held : {len(rates.get('quotes', []))}")
    if unzoned:
        print()
        for q in unzoned:
            print(f"  unusable as a zone cell: ${q['price_usd']:.2f} at {q['oz']} oz to "
                  f"{q['dest_zip']} — zone unknown"
                  + ("" if q.get("weighed") else ", and the weight was estimated not weighed"))
    print()

    if policy in (None, "flat"):
        print("POLICY 'flat' — one price per weight band, every destination")
        print("-" * 70)
        have = {b for (b, _z) in cells} | ({q["band"] for q in unzoned} if False else set())
        missing = [b for b in bands if b not in have]
        print(f"  need {len(bands)} quotes, hold 0, missing {len(missing)}")
        print("  A flat table cannot be right for more than one lane — it is what the site does")
        print("  today, and why a 90210 order and a next-town order pay the same.")
        if policy == "flat":
            for b in missing:
                why = f"   ({', '.join(sorted(set(reach.get(b, [])))[:3])})" if b in reach else ""
                print(f"    missing: {band_label(b):>11}{why}")
        print()

    if policy in (None, "banded"):
        print("POLICY 'banded' — near (1-4) / mid (5-6) / far (7-9)")
        print("-" * 70)
        need = len(bands) * len(BANDED)
        held = sum(1 for (b, z) in cells if b in bands)
        print(f"  need {need} quotes ({len(bands)} weights x {len(BANDED)} groups), "
              f"hold {held}, missing {need - held}")
        print("  Three quotes per weight instead of nine. Loses accuracy inside each group:")
        print("  a zone-1 buyer subsidises a zone-4 one.")
        if policy == "banded":
            for b in bands:
                gaps = [g for g, zs in BANDED.items() if not any((b, z) in cells for z in zs)]
                if gaps:
                    print(f"    {band_label(b):>11}: {', '.join(gaps)}")
        print()

    if policy in (None, "full"):
        print("POLICY 'full' — a price per weight band per zone 1-9")
        print("-" * 70)
        need = len(bands) * len(ZONES)
        held = sum(1 for (b, z) in cells if b in bands)
        print(f"  need {need} quotes ({len(bands)} weights x {len(ZONES)} zones), "
              f"hold {held}, missing {need - held}")
        if policy == "full":
            print()
            print("  " + "band".ljust(12) + "".join(f"z{z}".rjust(7) for z in ZONES))
            for b in bands:
                row = "  " + band_label(b).ljust(12)
                for z in ZONES:
                    q = cells.get((b, z))
                    row += (f"{q['price_usd']:.2f}".rjust(7) if q else "  —".rjust(7))
                print(row)
            print("\n  — = no quote. Nothing is inferred from a neighbouring cell.")
        print()

    if only_reachable:
        print("Weights the catalogue can actually produce")
        print("-" * 70)
        for b in bands:
            print(f"  {band_label(b):>11}  {', '.join(sorted(set(reach[b]))[:5])}")
        print()

    print("=" * 70)
    if not origin:
        print("BLOCKED: set origin_zip in shipping_rates.json first. Until then even the quote")
        print("already on file cannot be assigned to a zone, so no policy can be filled in.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
