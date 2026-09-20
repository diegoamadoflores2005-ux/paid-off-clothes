"""Validate a carrier quote the moment it is collected, and only then record it.

    python3 tools/record_quote.py --next
    python3 tools/record_quote.py --dest 98101 --oz 32 --box 12x12x11 \
        --line "USPS/Ground Advantage/7.42" --line "USPS/Ground Advantage Cubic/8.10"
    python3 tools/record_quote.py ... --record

Every problem this table has had was found by reading numbers back days later and noticing they
could not all be true: a UPS price filed as USPS, a Cubic price filed as weight-based, a 2 lb rate
above the 3 lb one, one figure carried across three pounds. None of those were hard to catch. They
were caught late because nothing checked a quote at the moment it was written down, when the person
who took it still had the screen in front of them.

So this refuses a quote that cannot be right, and says which rule it broke. Without --record it only
reports; with --record it writes the cell and its provenance. It never invents a rate, never fills a
neighbouring cell, and never records a quote that failed a check.

Give EVERY service line on the screen, not just the one you would buy. A single line cannot be
checked against anything; several lines diagnose themselves.
"""
import argparse
import json
import math
import os
import sys
import types

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RATES = os.path.join(APP_DIR, "shipping_rates.json")

# USPS fees that inflate a quote without the quoter noticing. Checked so a contaminated figure is
# rejected rather than recorded as if it were the clean rate.
NONSTANDARD_LENGTH_IN = 22       # $4.50 over this
OVERSIZE_VOLUME_CU_FT = 2.0      # $21.00 over this
DIM_WEIGHT_CU_IN = 1728          # dim weight above this, and only to zones 5-9
CUBIC_MAX_CU_FT = 1.0
CUBIC_MAX_SIDE_IN = 22


def load_orders():
    """db/orders.py from source — never through __pycache__, which has served stale bytecode here."""
    path = os.path.join(APP_DIR, "db", "orders.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    mod = types.ModuleType("orders_for_quotes")
    mod.__file__ = path
    exec(compile(src, path, "exec"), mod.__dict__)
    return mod


def parse_box(text):
    parts = [p for p in str(text).replace("*", "x").lower().split("x") if p.strip()]
    if len(parts) != 3:
        raise SystemExit(f"--box wants three dimensions, e.g. 12x12x11 (got {text!r})")
    try:
        return [float(p) for p in parts]
    except ValueError:
        raise SystemExit(f"--box dimensions must be numbers (got {text!r})")


def parse_line(text):
    bits = [b.strip() for b in str(text).split("/")]
    if len(bits) != 3:
        raise SystemExit(f'--line wants CARRIER/SERVICE/PRICE, e.g. "USPS/Ground Advantage/6.03" '
                         f'(got {text!r})')
    try:
        price = float(bits[2].lstrip("$"))
    except ValueError:
        raise SystemExit(f"price in {text!r} is not a number")
    return {"carrier": bits[0], "service": bits[1], "price_usd": round(price, 2)}


def box_facts(dims, zone):
    """Everything about the package that changes what a quote means."""
    v = dims[0] * dims[1] * dims[2]
    cu_ft = v / 1728.0
    longest = max(dims)
    fees = []
    if longest > NONSTANDARD_LENGTH_IN:
        fees.append(f"nonstandard length: longest side {longest:g} in is over {NONSTANDARD_LENGTH_IN} in")
    if cu_ft > OVERSIZE_VOLUME_CU_FT:
        fees.append(f"oversize volume: {cu_ft:.2f} cu ft is over {OVERSIZE_VOLUME_CU_FT} cu ft")
    if v > DIM_WEIGHT_CU_IN and zone is not None and zone >= 5:
        fees.append(f"dimensional weight: {v:.0f} cu in is over {DIM_WEIGHT_CU_IN} and zone {zone} "
                    f"is in the 5-9 range where it applies")
    cubic_ok = cu_ft <= CUBIC_MAX_CU_FT and longest <= CUBIC_MAX_SIDE_IN
    return {
        "cubic_in": round(v, 1),
        "cubic_ft": round(cu_ft, 3),
        "longest_in": longest,
        "cubic_eligible": cubic_ok,
        "cubic_tier_ft": round(math.ceil(cu_ft / 0.1) * 0.1, 1) if cubic_ok else None,
        "fees_triggered": fees,
    }


def check(args, rates, orders):
    """-> (problems, notes, chosen_line, cell_key). Problems mean nothing gets recorded."""
    problems, notes = [], []
    dims = parse_box(args.box)
    lines = [parse_line(t) for t in args.line]
    band = orders.band_for_oz(args.oz)
    zone = orders.zone_for_zip(args.dest, rates)
    group = orders.group_for_zone(zone, rates) if zone is not None else None

    if zone is None:
        problems.append(f"ZIP {args.dest} is not in zone_map — its zone is unknown, and a zone is "
                        f"never inferred from distance")
        return problems, notes, None, None
    notes.append(f"ZIP {args.dest} -> zone {zone} -> group {group!r}; {args.oz} oz -> band {band!r}")

    worst = orders.worst_case_zone(group, rates)
    if worst is not None and zone < worst:
        problems.append(f"{group!r} reaches zone {worst} but this was quoted to zone {zone}. One "
                        f"price per group must cover the group's dearest zone, or every order "
                        f"beyond it ships below cost.")

    facts = box_facts(dims, zone)
    notes.append(f"box {'x'.join(f'{d:g}' for d in dims)} = {facts['cubic_in']:.0f} cu in "
                 f"({facts['cubic_ft']:.2f} cu ft); Cubic "
                 + (f"eligible at the {facts['cubic_tier_ft']:.1f} cu ft tier"
                    if facts["cubic_eligible"] else "INELIGIBLE"))
    for f in facts["fees_triggered"]:
        problems.append(f"the package triggers a surcharge, so this price is not a clean rate — {f}")

    # Which line may fill a cell in THIS table.
    allowed = orders.allowed_services(rates)
    carrier = rates.get("carrier")
    eligible = [l for l in lines if l["carrier"] == carrier and l["service"] in allowed]
    if not eligible:
        got = ", ".join(f"{l['carrier']} {l['service']}" for l in lines) or "nothing"
        problems.append(f"no line on this screen may fill a cell. This table is {carrier} "
                        f"{'/'.join(allowed)} ({rates.get('rate_basis')} basis); you gave: {got}.")
        if any(l["service"] == "Ground Advantage Cubic" for l in lines) and facts["cubic_eligible"]:
            problems.append(f"Cubic won because this box is only {facts['cubic_ft']:.2f} cu ft. "
                            f"Cubic gets cheaper as volume falls — quote in a BIGGER box (under "
                            f"1.0 cu ft and under 22 in a side) to make the weight-based line win.")
        return problems, notes, None, None
    if len(eligible) > 1:
        problems.append("more than one line is eligible; give one price per service")
        return problems, notes, None, None
    chosen = eligible[0]

    # Agreement with what the ladder already says.
    table = rates.get("rate_table") or {}
    section = "core" if band in (table.get("core") or {}) else (
        "heavy" if band in (table.get("heavy") or {}) else None)
    if section is None:
        problems.append(f"band {band!r} has no row in the table; nothing to fill")
        return problems, notes, chosen, None
    cell = f"{section}.{band}.{group}"

    existing = (table[section][band] or {}).get(group)
    if existing is not None:
        if abs(existing - chosen["price_usd"]) < 0.005:
            notes.append(f"{cell} already holds ${existing:.2f} — this quote agrees with it")
        else:
            problems.append(f"{cell} already holds ${existing:.2f}, and this quote says "
                            f"${chosen['price_usd']:.2f}. One of them is wrong; recording would "
                            f"bury the question. Re-quote both weights in one sitting.")

    # Monotonicity, checked against the real neighbours rather than after the fact.
    probe = json.loads(json.dumps(rates))
    probe["rate_table"][section][band][group] = chosen["price_usd"]
    for p in orders.rate_table_problems(probe):
        if group in p:
            problems.append(f"it would break the ladder — {p}")

    return problems, notes, chosen, cell


def cmd_next(rates, orders):
    """The next quote worth collecting, with everything needed to take it."""
    table = rates.get("rate_table") or {}
    carrier, basis = rates.get("carrier"), rates.get("rate_basis")
    print(f"Table: {carrier} {'/'.join(orders.allowed_services(rates))}  ({basis} basis)")
    print(f"Origin: {rates.get('origin_zip')}\n")
    todo = [(("core", b, g))
            for b in (table.get("core") or {})
            for g in ("near", "mid", "far")
            if (table["core"][b] or {}).get(g) is None]
    if not todo:
        print("Every core cell is filled.")
        return 0
    by_group = {}
    for _s, b, g in todo:
        by_group.setdefault(g, []).append(b)
    zm = rates.get("zone_map") or {}
    for g in ("near", "mid", "far"):
        if g not in by_group:
            continue
        worst = orders.worst_case_zone(g, rates)
        examples = sorted(p for p, z in zm.items() if z == worst)[:8]
        print(f"  {g}: {len(by_group[g])} cells missing — bands {', '.join(by_group[g])}")
        print(f"     must be quoted to zone {worst} (the dearest in this group).")
        print(f"     prefixes in that zone: {', '.join(examples)}")
    print("\nQuote in a box that is UNDER 1.0 cu ft and UNDER 22 in on every side, but as close to")
    print("1.0 cu ft as you can get — that puts Cubic at its dearest so the weight-based line wins,")
    print("without triggering dimensional weight or a nonstandard-length fee.")
    print("\nRecord every service line on the screen, not only the one you would buy.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--next", action="store_true", help="what to collect next, and how")
    ap.add_argument("--dest", help="destination ZIP")
    ap.add_argument("--oz", type=float, help="package weight in ounces")
    ap.add_argument("--box", help="dimensions in inches, e.g. 12x12x11")
    ap.add_argument("--line", action="append", default=[],
                    help='one service line: "CARRIER/SERVICE/PRICE". Repeat for every line shown.')
    ap.add_argument("--record", action="store_true", help="write it, if every check passes")
    args = ap.parse_args()

    with open(RATES, encoding="utf-8") as fh:
        rates = json.load(fh)
    orders = load_orders()

    if args.next or not (args.dest and args.oz and args.box and args.line):
        return cmd_next(rates, orders)

    problems, notes, chosen, cell = check(args, rates, orders)
    for n in notes:
        print(f"  {n}")
    if chosen:
        print(f"  eligible line: {chosen['carrier']} {chosen['service']} ${chosen['price_usd']:.2f}")
    print()
    if problems:
        print("REFUSED — nothing written:")
        for p in problems:
            print(f"  ! {p}")
        return 1
    print(f"OK — {cell} would take ${chosen['price_usd']:.2f}")
    if not args.record:
        print("  (dry run; pass --record to write it)")
        return 0

    section, band, group = cell.split(".")
    rates["rate_table"][section][band][group] = chosen["price_usd"]
    rates.setdefault("cell_provenance", {})[cell] = {
        "verified": True,
        "service": chosen["service"],
        "rate_basis": rates.get("rate_basis"),
        "price_usd": chosen["price_usd"],
        "dest_zip": args.dest,
        "oz": args.oz,
        "dims_in": parse_box(args.box),
        "all_lines_seen": [parse_line(t) for t in args.line],
        "evidence": (f"Quoted at {args.oz} oz to {args.dest} (zone "
                     f"{orders.zone_for_zip(args.dest, rates)}) in a "
                     f"{'x'.join(f'{d:g}' for d in parse_box(args.box))} box, with every service "
                     f"line on the screen recorded. Validated by tools/record_quote.py against "
                     f"zone worst-case, package surcharges, service eligibility and ladder "
                     f"monotonicity before being written."),
    }
    with open(RATES, "w", encoding="utf-8") as fh:
        json.dump(rates, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"  recorded {cell}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
