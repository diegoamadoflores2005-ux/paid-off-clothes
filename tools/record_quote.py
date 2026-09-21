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
# How far a quote may fall below the smallest discount seen on a VERIFIED cell in the same band
# before it is treated as a different product. Generous, because one or two peers is thin evidence.
DISCOUNT_TOLERANCE = 0.08

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


def zone_of(entry, rates, orders):
    """The zone a recorded cell was quoted to, from its own dest_zip."""
    return orders.zone_for_zip(entry.get("dest_zip"), rates)


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
    anomaly = getattr(args, "anomaly", None)
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

    # Against the published advertised rate, where one exists for this band and zone.
    #
    # This account runs well below Commercial Pricing — both verified sub-1-lb cells sit about 22%
    # under the advertised figure. A quote AT or ABOVE advertised is impossible for it, and one only
    # a few percent below is far more likely to be a retail line than a real rate. That is the shape
    # the 1 lb zone 6 quote made: $9.24 against $9.63 advertised, a 4% discount where everything
    # else shows 22%.
    adv = ((rates.get("advertised_reference") or {}).get("rates_by_zone") or {})
    ceiling = (adv.get(band) or {}).get(str(zone))
    if ceiling:
        off = 1.0 - chosen["price_usd"] / ceiling
        if off <= 0:
            problems.append(
                f"${chosen['price_usd']:.2f} is at or above the ADVERTISED rate of ${ceiling:.2f} "
                f"for {band} lb to zone {zone}. This account prices below Commercial, so it cannot "
                f"pay more than the advertised figure — that is a retail or wrong-service line.")
        else:
            # Compare like with like. The discount is NOT uniform across bands: the July 2026
            # change made sub-1-lb flat and cut it much harder than the pound bands, so a sub-1-lb
            # cell sits ~22% below advertised while 1 lb sits far less. An earlier version pooled
            # every band into one expected discount and refused a 1 lb quote for being "only 4%
            # off" — comparing it against sub-1-lb evidence it had nothing to do with. Only a
            # verified cell in the SAME band is evidence about that band.
            peers = [e for key, e in (rates.get("cell_provenance") or {}).items()
                     if e.get("verified") and key.split(".")[1] == band and e.get("price_usd")
                     and (adv.get(band) or {}).get(str(zone_of(e, rates, orders)))]
            offs = []
            for e in peers:
                peer_ceiling = adv[band][str(zone_of(e, rates, orders))]
                offs.append(1.0 - e["price_usd"] / peer_ceiling)
            if offs and off < min(offs) - DISCOUNT_TOLERANCE:
                problems.append(
                    f"${chosen['price_usd']:.2f} is {off * 100:.1f}% below the advertised "
                    f"${ceiling:.2f}, but every verified {band} lb cell sits at least "
                    f"{min(offs) * 100:.1f}% below. Same band, very different discount — check the "
                    f"service line.")

    # A quote that lands exactly on another band's price, to the cent, is far more likely to be
    # that band than a coincidence. This is the shape the 12x12x11 control test made: entered as
    # 2 lb, it returned $7.03, which is precisely the verified 5 lb rate. The weight field was not
    # visible in the screenshot. Carriers do not price two bands identically by accident on a
    # rising ladder, so the cheap explanation is a stale or mistyped weight.
    twins = []
    for other_band, row in (table.get(section) or {}).items():
        if other_band == band:
            continue
        if (row or {}).get(group) is not None and abs(row[group] - chosen["price_usd"]) < 0.005:
            twins.append(other_band)
    if twins:
        problems.append(
            f"${chosen['price_usd']:.2f} is exactly the price already recorded for band "
            f"{', '.join(twins)} in this group. Two bands share a price for one of two reasons, "
            f"and neither belongs in a weight-based table: a stale weight field from the previous "
            f"quote, or Ground Advantage CUBIC winning the rate shop — cubic is flat across weight, "
            f"so once it takes over, every band above returns the same figure. Check the weight "
            f"field said {args.oz:g} oz and check which service the line names.")

    # The bracket its filled neighbours already impose on this band.
    #
    # Monotonicity catches the same faults, but it reports them as "band X is cheaper than band Y",
    # which names one of the two bands and leaves you to work out which is wrong. A bracket says the
    # actionable thing directly: this band must lie between these two figures, and yours does not.
    # The 1 lb zone 6 case is exactly that — $6.07 below it and $8.17 above it leave no room for
    # $9.24, whatever $9.24 turns out to be, and that holds without any theory about its origin.
    want_lb = 0 if band == "sub" else int(band)
    below = above = None
    for lb, sec, key in orders.table_bands(table):
        # NOT `cell` — that name already holds the cell key this function returns, and shadowing it
        # made the tool report the cell as None while still passing every check.
        neighbour = ((table.get(sec) or {}).get(key) or {}).get(group)
        if neighbour is None or key == band:
            continue
        if lb < want_lb and (below is None or lb > below[0]):
            below = (lb, key, float(neighbour))
        if lb > want_lb and (above is None or lb < above[0]):
            above = (lb, key, float(neighbour))
    lo = below[2] if below else None
    hi = above[2] if above else None
    price = chosen["price_usd"]
    if (lo is not None and price < lo) or (hi is not None and price > hi):
        bounds = []
        if below:
            bounds.append(f"at least ${lo:.2f} (the {below[1]} lb rate)")
        if above:
            bounds.append(f"at most ${hi:.2f} (the {above[1]} lb rate)")
        msg = (f"band {band} must be " + " and ".join(bounds) +
               f", and ${price:.2f} is outside that. Postage normally rises with weight, so the "
               f"neighbours already filled leave no room for this figure.")
        if anomaly:
            notes.append(f"BRACKET OVERRIDDEN — {msg}")
            notes.append(f"  accepted as a verified anomaly: {anomaly}")
        else:
            problems.append(msg + " If the weight and price were both read off the screen and it "
                                  "still reads this way, pass --anomaly with the evidence.")

    # Monotonicity, checked against the real neighbours rather than after the fact.
    probe = json.loads(json.dumps(rates))
    probe["rate_table"][section][band][group] = chosen["price_usd"]
    for p in orders.rate_table_problems(probe):
        if group not in p:
            continue
        if anomaly:
            notes.append(f"LADDER INVERSION ACCEPTED — {p}")
        else:
            problems.append(f"it would break the ladder — {p}")

    return problems, notes, chosen, cell


# ---------- a whole quoting session at once -------------------------------------------------------
# Collecting 37 cells one command at a time is 37 chances to mistype a flag. A session file matches
# how the work is actually done: pick a destination, pick a box, then walk the weights. Every row is
# validated and NOTHING is written unless every row passes — the same all-or-nothing rule
# apply_shipping_rates.py uses, for the same reason. A half-applied column is worse than none,
# because the gaps are invisible once the file looks full.
SESSION_HELP = """\
# Lines starting with # are ignored. `dest` and `box` apply to every row below them.
dest 98101
box 12x12x11

# weight_oz   then every service line on the screen, as CARRIER/SERVICE/PRICE
8    USPS/Ground Advantage/7.42, USPS/Ground Advantage Cubic/8.10
32   USPS/Ground Advantage/8.15, USPS/Ground Advantage Cubic/8.10
"""


def parse_session(text):
    """-> [{dest, oz, box, line[]}], or raise SystemExit naming the offending line."""
    rows, dest, box = [], None, None
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        head, _, rest = line.partition(" ")
        key = head.lower()
        if key == "dest":
            dest = rest.strip()
            continue
        if key == "box":
            box = rest.strip()
            continue
        if dest is None or box is None:
            raise SystemExit(f"line {n}: a weight row before `dest` and `box` have both been set")
        try:
            oz = float(head)
        except ValueError:
            raise SystemExit(f"line {n}: expected `dest`, `box`, or a weight in ounces, got {head!r}")
        # Comma-separated, because service names contain spaces ("Ground Advantage Cubic").
        services = [t.strip() for t in rest.split(",") if "/" in t]
        if not services:
            raise SystemExit(f"line {n}: no service lines. Give every line on the screen as "
                             f"CARRIER/SERVICE/PRICE, separated by commas, e.g.\n"
                             f"  32   USPS/Ground Advantage/6.03, USPS/Ground Advantage Cubic/7.10")
        rows.append({"dest": dest, "oz": oz, "box": box, "line": services, "lineno": n})
    return rows


def cmd_session(path, rates, orders, write):
    """Validate every row first, then write all of them or none."""
    if path == "-":
        text = sys.stdin.read()
    else:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    rows = parse_session(text)
    if not rows:
        print("No quote rows found. The format is:\n")
        print(SESSION_HELP)
        return 2

    staged, failed = [], 0
    probe = json.loads(json.dumps(rates))   # rows are checked against each other, not just the file
    for row in rows:
        args = argparse.Namespace(dest=row["dest"], oz=row["oz"], box=row["box"],
                                  line=row["line"], record=False, next=False, anomaly=None)
        problems, _notes, chosen, cell = check(args, probe, orders)
        label = f"line {row['lineno']}: {row['oz']:g} oz -> {row['dest']}"
        if problems:
            failed += 1
            print(f"  FAIL  {label}")
            for pr in problems:
                print(f"        ! {pr}")
            continue
        print(f"  ok    {label}  {cell} = ${chosen['price_usd']:.2f}")
        section, band, group = cell.split(".")
        probe["rate_table"][section][band][group] = chosen["price_usd"]
        staged.append((cell, chosen, args))

    print()
    if failed:
        print(f"REFUSED — {failed} of {len(rows)} rows failed. Nothing written.")
        print("  A column written with gaps looks complete and is not, so the whole session is")
        print("  held back until every row passes.")
        return 1
    print(f"All {len(staged)} rows pass.")
    if not write:
        print("  (dry run; pass --record to write them)")
        return 0

    for cell, chosen, args in staged:
        section, band, group = cell.split(".")
        rates["rate_table"][section][band][group] = chosen["price_usd"]
        rates.setdefault("cell_provenance", {})[cell] = provenance_for(chosen, args, rates, orders)
    with open(RATES, "w", encoding="utf-8") as fh:
        json.dump(rates, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"  recorded {len(staged)} cells")
    return 0


def provenance_for(chosen, args, rates, orders):
    return {
        "verified": True,
        "service": chosen["service"],
        "rate_basis": rates.get("rate_basis"),
        "price_usd": chosen["price_usd"],
        "dest_zip": args.dest,
        "oz": args.oz,
        "dims_in": parse_box(args.box),
        "all_lines_seen": [parse_line(t) for t in args.line],
        "evidence": (f"Quoted at {args.oz:g} oz to {args.dest} (zone "
                     f"{orders.zone_for_zip(args.dest, rates)}) in a "
                     f"{'x'.join(f'{d:g}' for d in parse_box(args.box))} box, with every service "
                     f"line on the screen recorded. Validated by tools/record_quote.py against "
                     f"zone worst-case, package surcharges, service eligibility and ladder "
                     f"monotonicity before being written."),
    }


def cmd_check(dest, rates, orders):
    """Is this destination the right one to quote, and for which group? Run BEFORE quoting.

    A three-digit prefix is not a place. Birmingham AL is 352 and Tuscaloosa AL is 354; they are an
    hour apart and in different zone groups. Substituting a city you recognise for a prefix off a
    list is how a whole column gets quoted to the wrong zone — which is exactly what happened, and
    the quoting had already been done before anything noticed.
    """
    digits = "".join(c for c in str(dest) if c.isdigit())
    zone = orders.zone_for_zip(dest, rates)
    print(f"  {dest}  ->  prefix {digits[:3] or '???'}")
    if zone is None:
        print("  NOT IN THE ZONE MAP. Its zone is unknown and is never inferred from distance.")
        print("  Pick a destination whose prefix appears in the chart; run --next for examples.")
        return 1
    group = orders.group_for_zone(zone, rates)
    worst = orders.worst_case_zone(group, rates)
    print(f"  zone {zone}  ->  group {group!r}  (that group spans "
          f"{', '.join(str(z) for z in rates['zone_groups'][group]['zones'])})")
    if zone == worst:
        print(f"  OK — zone {zone} is {group!r}'s dearest zone, so a quote here covers the group.")
        missing = [b for b, row in (rates.get("rate_table", {}).get("core") or {}).items()
                   if (row or {}).get(group) is None]
        print(f"  {group!r} still needs: {', '.join(missing) if missing else 'nothing'}")
        return 0
    print(f"  WRONG DESTINATION for filling cells. {group!r} reaches zone {worst}, and one price "
          f"covers the whole group,")
    print(f"  so quoting at zone {zone} would ship every zone {worst} order below cost.")
    examples = sorted(p for p, z in (rates.get("zone_map") or {}).items() if z == worst)[:10]
    print(f"  Quote to a ZIP starting with one of these instead: {', '.join(examples)}")
    return 1


def cmd_next(rates, orders):
    """The next quote worth collecting, with everything needed to take it."""
    table = rates.get("rate_table") or {}
    carrier, basis = rates.get("carrier"), rates.get("rate_basis")
    print(f"Table: {carrier} {'/'.join(orders.allowed_services(rates))}  ({basis} basis)")
    print(f"Origin: {rates.get('origin_zip')}\n")
    # ONE SESSION FIRST. The columns are not equal: 'far' is over half the map and prices nothing,
    # while a band nobody's stock can reach buys no orders at all. Leading with a flat list of every
    # empty cell is what makes this look like 26 errands instead of one sitting.
    cov = orders.coverage_report(rates)
    light = [b for b in ("sub", "2", "3", "5") if b in set(orders.required_bands(rates))]
    first = next((r for r in cov if r["quotes_to_complete"]), None)
    if first:
        worst = orders.worst_case_zone(first["group"], rates)
        want = [b for b in light if b in first["missing_bands"]] or first["missing_bands"][:4]
        state = (f"prices nothing above {first['priced_to_oz'] / 16:.2f} lb"
                 if first["priced_to_oz"] else "has no verified rate at all")
        print(f"START HERE — one session, {len(want)} rows, group '{first['group']}' "
              f"({first['share'] * 100:.0f}% of the map, {state})"
              f"\n     bands {', '.join(want)} to zone {worst}. Those cover a single piece up to")
        print("     roughly ten, which is the shape of almost every order this shop takes.")
        print("     Everything else on this list can wait: it is already a manual quote, which")
        print("     costs an email rather than a sale.\n")

    groups = orders.group_names(rates)
    todo = [(("core", b, g))
            for b in (table.get("core") or {})
            for g in groups
            if (table["core"][b] or {}).get(g) is None]
    if not todo:
        print("Every core cell is filled.")
        return 0
    by_group = {}
    for _s, b, g in todo:
        by_group.setdefault(g, []).append(b)
    zm = rates.get("zone_map") or {}
    print("EVERYTHING OUTSTANDING (none of it blocks launch):")
    for r in cov:
        g = r["group"]
        if g not in by_group:
            continue
        worst = orders.worst_case_zone(g, rates)
        examples = sorted(p for p, z in zm.items() if z == worst)[:8]
        print(f"  {g}: {len(by_group[g])} cells missing — bands {', '.join(by_group[g])}")
        print(f"     must be quoted to zone {worst} (the dearest in this group).")
        print(f"     use a ZIP whose FIRST THREE DIGITS are one of: {', '.join(examples)}")
        print(f"     e.g. {examples[0]}01 — do NOT substitute a city you recognise; neighbouring")
        print(f"     prefixes fall in different groups (Birmingham 352 is far, Tuscaloosa 354 is mid).")
        print(f"     Check any destination first:  python3 tools/record_quote.py --check <ZIP>")
    print("\nQuote in a box that is UNDER 1.0 cu ft and UNDER 22 in on every side, but as close to")
    print("1.0 cu ft as you can get — that puts Cubic at its dearest so the weight-based line wins,")
    print("without triggering dimensional weight or a nonstandard-length fee.")
    print("\nRecord every service line on the screen, not only the one you would buy.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--next", action="store_true", help="what to collect next, and how")
    ap.add_argument("--check", metavar="ZIP",
                    help="is this destination right for filling cells? run it before quoting")
    ap.add_argument("--dest", help="destination ZIP")
    ap.add_argument("--oz", type=float, help="package weight in ounces")
    ap.add_argument("--box", help="dimensions in inches, e.g. 12x12x11")
    ap.add_argument("--line", action="append", default=[],
                    help='one service line: "CARRIER/SERVICE/PRICE". Repeat for every line shown.')
    ap.add_argument("--record", action="store_true", help="write it, if every check passes")
    ap.add_argument("--anomaly", metavar="REASON",
                    help="accept a verified quote that inverts the ladder, recording REASON. The "
                         "tariff really can invert where the discount is uneven between bands. Use "
                         "ONLY when the weight and price were both read off the screen.")
    ap.add_argument("--session", help="a file (or - for stdin) holding a whole quoting session; "
                                      "every row is validated and all are written or none")
    args = ap.parse_args()

    with open(RATES, encoding="utf-8") as fh:
        rates = json.load(fh)
    orders = load_orders()

    if args.check:
        return cmd_check(args.check, rates, orders)
    if args.session:
        return cmd_session(args.session, rates, orders, args.record)
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
    prov = provenance_for(chosen, args, rates, orders)
    if getattr(args, "anomaly", None):
        prov["anomaly"] = args.anomaly
        # Record the inverted PAIR so rate_table_problems stops flagging just this one, rather
        # than the monotonicity rule being switched off everywhere.
        table = rates.get("rate_table") or {}
        want_lb = 0 if band == "sub" else int(band)
        for lb, sec, key in orders.table_bands(table):
            neighbour = ((table.get(sec) or {}).get(key) or {}).get(group)
            if neighbour is None or key == band:
                continue
            if lb > want_lb and float(neighbour) < chosen["price_usd"]:
                pair = [group, band, key]
                if pair not in rates.setdefault("verified_anomalies", []):
                    rates["verified_anomalies"].append(pair)
                break
    rates.setdefault("cell_provenance", {})[cell] = prov
    with open(RATES, "w", encoding="utf-8") as fh:
        json.dump(rates, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"  recorded {cell}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
