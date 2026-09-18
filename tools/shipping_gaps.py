"""Which carrier quotes are still missing, exactly — and what to type into Pirate Ship.

    python3 tools/shipping_gaps.py              what is filled, what is empty
    python3 tools/shipping_gaps.py --worklist   the quotes to collect, in the order to collect them
    python3 tools/shipping_gaps.py --why        why each band is on the list

Reads shipping_rates.json. It never fills a cell, never interpolates between two quotes, and never
reuses a price from one zone group in another. A quote for 8 oz to one ZIP says nothing about 3 lb
and nothing about a different zone; treating it as if it did is how a shop ends up paying postage
out of its own margin on every distant order.
"""
import json
import os
import sys
import types

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RATES = os.path.join(APP_DIR, "shipping_rates.json")
GROUPS = ("near", "mid", "far")

# Why each band is on the worklist. Written from the catalogue, not guessed — see the notes in
# STRIPE.md for how these were derived.
WHY = {
    "sub": "1 shirt or 1 belt — the single-item order",
    "2": "2-4 shirts, 2 belts",
    "3": "5-6 shirts, 1 pair shoes, 1 bag, 3-4 belts",
    "4": "7-8 shirts, 5-6 belts",
    "5": "9-10 shirts, 2 bags",
    "6": "2 pairs shoes, 8 belts",
    "7": "3 bags, 10 belts",
    "8": "3 pairs shoes",
    "9": "20 SHIRTS — the deepest advertised bulk tier. Below this the 20+ price is unshippable.",
    "11": "4 pairs shoes, 5 bags",
    "13": "5 pairs shoes, 6 bags",
    "16": "6 pairs shoes, 8 bags",
}


def band_label(b):
    return "under 1 lb" if b == "sub" else f"{b} lb"


def band_oz(b):
    return "≤15.99" if b == "sub" else str(int(b) * 16)


def load():
    with open(RATES, encoding="utf-8") as fh:
        return json.load(fh)


def cells(rates):
    """Every cell in the table as (section, band, group, price)."""
    out = []
    table = rates.get("rate_table", {})
    for section in ("core", "heavy"):
        for band, row in table.get(section, {}).items():
            for g in GROUPS:
                out.append((section, band, g, row.get(g)))
    for g in GROUPS:
        out.append(("over_max", "over", g, table.get("over_max", {}).get(g)))
    return out


def main():
    args = sys.argv[1:]
    rates = load()
    origin = rates.get("origin_zip")
    policy = rates.get("zone_policy")
    all_cells = cells(rates)
    filled = [c for c in all_cells if c[3] is not None]
    empty = [c for c in all_cells if c[3] is None]

    print("Shipping quotes — Paid Off Clothes")
    print("=" * 72)
    print(f"carrier     : {rates.get('carrier')}")
    print(f"origin ZIP  : {origin or 'NOT SET'}")
    print(f"zone policy : {policy}")
    for g in GROUPS:
        zs = rates.get("zone_groups", {}).get(g, {})
        print(f"  {g:<5} zones {', '.join(map(str, zs.get('zones', []))):<10} {zs.get('note','')}")
    print()

    ref = [q for q in rates.get("quotes", []) if q.get("use") == "reference only"]
    if ref:
        print("Held as reference, deliberately not in the table:")
        for q in ref:
            if q.get("zone") is not None and q.get("zone_verified"):
                zone = f"zone {q['zone']} confirmed"
            elif q.get("zone_inferred"):
                zone = f"zone {q['zone_inferred']} inferred, UNCONFIRMED"
            else:
                zone = "zone unknown"
            weighed = {True: "weighed", False: "weight ESTIMATED, not weighed"}.get(
                q.get("weighed"), "weighed: not stated")
            print(f"  ${q['price_usd']:.2f} at {q['oz']} oz to {q['dest_zip']} — {zone}; {weighed}")
        print()

    # Side-by-side carrier quotes for one lane. Only the row matching this file's `carrier` can
    # ever reach the table — a ladder built from two carriers prices nothing anyone can buy.
    for cmp in rates.get("rate_comparisons", []):
        dims = cmp.get("dims_in_stated")
        dims_s = "x".join(str(d) for d in dims) + " in (estimated)" if dims else "no dimensions"
        print(f"Same lane, {cmp['oz']} oz to {cmp['dest_zip']} (zone {cmp['zone']}), {dims_s}:")
        for q in sorted(cmp.get("quotes", []), key=lambda q: q["price_usd"]):
            mark = "  <- table" if q["carrier"] in (rates.get("carrier") or "") and \
                q["service"] in (rates.get("carrier") or "") else ""
            print(f"  ${q['price_usd']:>5.2f}  {q['carrier']} {q['service']}{mark}")
        print()

    # A conflict is the kind of thing that must be in front of you every time, not filed away.
    # A *resolved* one is the opposite: shouting about it every run buries the open ones, so it
    # collapses to a single line and keeps its full reasoning in the file.
    for c in rates.get("_conflicts", []):
        status = c.get("status", "")
        if status.startswith("RESOLVED"):
            print(f"   resolved: {c['cell']} — {status[len('RESOLVED'):].lstrip(' -—')}")
            continue
        print(f"!! CONFLICT in {c['cell']}: {' vs '.join(c['quotes'])}")
        for line in c.get("why_it_matters", []):
            print(f"   {line}")
        if c.get("to_resolve"):
            print(f"   NEXT: {c['to_resolve']}")
        if c.get("meanwhile"):
            print(f"   meanwhile: {c['meanwhile']}")
        print()

    print(f"cells filled : {len(filled)} of {len(all_cells)}")
    print(f"cells empty  : {len(empty)}")
    print()

    # the grid
    table = rates.get("rate_table", {})
    print("  " + "band".ljust(13) + "oz".rjust(7) + "".join(g.rjust(9) for g in GROUPS))
    for section in ("core", "heavy"):
        for band, row in table.get(section, {}).items():
            line = "  " + band_label(band).ljust(13) + band_oz(band).rjust(7)
            for g in GROUPS:
                v = row.get(g)
                line += (f"{v:.2f}".rjust(9) if v is not None else "—".rjust(9))
            print(line + ("" if section == "core" else "   (heavy)"))
    line = "  " + "over top".ljust(13) + "—".rjust(7)
    for g in GROUPS:
        v = table.get("over_max", {}).get(g)
        line += (f"{v:.2f}".rjust(9) if v is not None else "—".rjust(9))
    print(line + "   (fallback)")
    print("\n  — = no quote. Nothing is inferred from a neighbouring cell.")

    if "--worklist" in args or "--why" in args:
        print()
        print("Worklist — one Pirate Ship quote per line")
        print("-" * 72)
        print(f"Set origin to {origin}. For each group pick ONE destination, read the zone Pirate")
        print("Ship prints, and use that same destination for every weight in the group.")
        print()
        n = 0
        for section in ("core", "heavy"):
            if section == "heavy":
                print("\n  --- heavy: bulk shoes and bags only. Skip if you cap those orders. ---")
            for band in table.get(section, {}):
                missing = [g for g in GROUPS if table[section][band].get(g) is None]
                if not missing:
                    continue
                n += len(missing)
                why = f"   {WHY.get(band, '')}" if "--why" in args else ""
                print(f"  {band_label(band):<12} at {band_oz(band):>6} oz   -> {', '.join(missing)}{why}")
        over_missing = [g for g in GROUPS if table.get("over_max", {}).get(g) is None]
        if over_missing:
            n += len(over_missing)
            print(f"\n  {'over top band':<12}            -> {', '.join(over_missing)}"
                  + ("   quote the heaviest order you will accept; this covers everything above"
                     if "--why" in args else ""))
        print(f"\n  {n} quotes to collect.")

    print()
    print("=" * 72)
    if empty:
        print("Nothing drives the site from this file yet — the flat ladder is still live.")
        print("Enter quotes, then run: python3 tools/apply_shipping_rates.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
