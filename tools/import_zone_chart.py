"""Turn a pasted USPS Domestic Zone Chart into shipping_rates.json's zone_map.

    python3 tools/import_zone_chart.py chart.csv          # a file you downloaded or pasted into
    pbpaste | python3 tools/import_zone_chart.py -        # straight off the clipboard (macOS)
    python3 tools/import_zone_chart.py chart.csv --dry-run

Get the chart from postcalc.usps.com/DomesticZoneChart with origin **856** (Vail, AZ). It lists
every destination ZIP prefix and the zone it falls in from that origin. That mapping is data USPS
publishes — it is not computable from distance, so this imports it rather than deriving it.

Accepts the shapes the chart comes in, because the site has changed format before:

    850       1          a single prefix
    850-853   1          an inclusive range of prefixes
    850,851   1          a comma list
    00501     8          a full 5-digit ZIP (first three digits are used)

Separators can be commas, tabs or runs of spaces. Header rows, blank lines and anything without a
prefix-and-zone pair are skipped and counted, so a format change shows up as "skipped" rather than
as silently missing prefixes.

It refuses to write a chart that looks wrong — a zone outside 1-9, or so few prefixes that the
paste was clearly truncated — because a partial map means some buyers are quoted by zone and
others fall back, with nothing on the page to say so.
"""
import json
import os
import re
import sys

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RATES = os.path.join(APP_DIR, "shipping_rates.json")

# The real chart for one origin covers most of the ~900 live prefixes. Well under that means a
# truncated copy-paste, which is the failure worth catching — it would look like a working import.
MIN_SANE_PREFIXES = 400

ROW = re.compile(r"^\s*(\d{3,5})\s*(?:-|–|to)?\s*(\d{3,5})?\s*[,\t; ]+\s*(\d)\s*$")


def parse(text):
    """-> (mapping, skipped_lines, problems). Never guesses a zone for a prefix it did not see."""
    mapping, skipped, problems = {}, [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # A comma list of prefixes sharing one zone: "850,851,852  4"
        listed = re.match(r"^\s*((?:\d{3,5}\s*,\s*)+\d{3,5})\s*[,\t; ]+\s*(\d)\s*$", line)
        if listed:
            zone = int(listed.group(2))
            for p in re.findall(r"\d{3,5}", listed.group(1)):
                _put(mapping, p, zone, problems)
            continue

        m = ROW.match(line)
        if not m:
            skipped.append(raw)
            continue
        start, end, zone = m.group(1), m.group(2), int(m.group(3))
        if end is None:
            _put(mapping, start, zone, problems)
        else:
            a, b = int(start[:3]), int(end[:3])
            if b < a:
                problems.append(f"range runs backwards: {line}")
                continue
            if b - a > 200:
                problems.append(f"range is implausibly wide, ignored: {line}")
                continue
            for p in range(a, b + 1):
                _put(mapping, f"{p:03d}", zone, problems)
    return mapping, skipped, problems


def _put(mapping, prefix, zone, problems):
    p = prefix[:3]
    if not 1 <= zone <= 9:
        problems.append(f"zone {zone} for {p} is outside 1-9")
        return
    if p in mapping and mapping[p] != zone:
        # Two different zones for one prefix means the paste mixed two origins, or two charts.
        problems.append(f"prefix {p} appears as both zone {mapping[p]} and zone {zone}")
        return
    mapping[p] = zone


def main():
    args = [a for a in sys.argv[1:]]
    dry = "--dry-run" in args
    args = [a for a in args if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2

    text = sys.stdin.read() if args[0] == "-" else open(args[0], encoding="utf-8").read()
    mapping, skipped, problems = parse(text)

    print(f"prefixes parsed : {len(mapping)}")
    by_zone = {}
    for p, z in mapping.items():
        by_zone[z] = by_zone.get(z, 0) + 1
    for z in sorted(by_zone):
        print(f"  zone {z}: {by_zone[z]:>4} prefixes")
    if skipped:
        print(f"lines skipped   : {len(skipped)}  (first: {skipped[0][:60]!r})")
    for p in problems[:10]:
        print(f"  PROBLEM: {p}")
    if len(problems) > 10:
        print(f"  ... and {len(problems) - 10} more")

    if problems:
        print("\nNothing written. Fix the source and run again.")
        return 1
    if len(mapping) < MIN_SANE_PREFIXES:
        print(f"\nOnly {len(mapping)} prefixes — a full chart for one origin has many more.")
        print("That usually means a truncated paste. Nothing written; a partial map would quote")
        print("some buyers by zone and drop the rest to the flat ladder with no sign on the page.")
        return 1

    rates = json.load(open(RATES, encoding="utf-8"))
    origin = rates.get("origin_zip", "")
    home = mapping.get(origin[:3]) if origin else None
    if home is not None and home != 1:
        print(f"\n  note: the origin's own prefix {origin[:3]} maps to zone {home}, not 1.")
        print("  Worth a glance — it usually means the chart came from a different origin.")

    if dry:
        sample = dict(list(sorted(mapping.items()))[:6])
        print(f"\nDry run. Would write {len(mapping)} prefixes, e.g. {sample}")
        return 0

    rates["zone_map"] = dict(sorted(mapping.items()))
    rates["zone_map_source"] = {
        "chart": "USPS Domestic Zone Chart",
        "origin_prefix": origin[:3] if origin else None,
        "prefixes": len(mapping),
        "imported_from": os.path.basename(args[0]) if args[0] != "-" else "stdin",
    }
    json.dump(rates, open(RATES, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    open(RATES, "a", encoding="utf-8").write("\n")
    print(f"\nWrote {len(mapping)} prefixes to shipping_rates.json.")
    print("Zone pricing still needs every rate cell filled — python3 tools/shipping_gaps.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
