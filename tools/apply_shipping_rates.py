"""Apply real carrier quotes to the shipping ladder, in all three places, or not at all.

    python3 tools/apply_shipping_rates.py --dry-run  sub=5.75 1=8.10 2=9.05 ... over=24.00
    python3 tools/apply_shipping_rates.py --quotes quotes.json
    python3 tools/apply_shipping_rates.py --show

The rate ladder lives in three files that must agree: script.js quotes the buyer, db/orders.py
charges the card, and tests/test_shipping_weights.py records which pounds still have no band. Hand
-editing three files in step is how the last shipping bug happened, so this does it in one move.

NOTHING IS WRITTEN UNLESS THE TESTS PASS. Files are edited, both suites run, and if either fails
every file is restored and the failure is printed. A half-applied ladder — a rate in the JS that
the Python does not charge — is worse than no change at all.

Rates are quotes YOU obtain from Pirate Ship for YOUR origin and zone. This tool never invents one;
it validates the shape of what you give it and refuses anything that cannot be right.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(APP_DIR, "script.js")
PY = os.path.join(APP_DIR, "db", "orders.py")
TEST = os.path.join(APP_DIR, "tests", "test_shipping_weights.py")
SUITES = [os.path.join(APP_DIR, "tests", "test_shipping_weights.py"),
          os.path.join(APP_DIR, "tests", "test_stripe_checkout.py")]

SUB_POUND_OZ = 15.99
MAX_POUNDS = 16          # 6 pairs of shoes; nothing in the catalogue goes heavier

# A band's key is the pound it covers; "sub" is the flat under-1-lb price and "over" the fallback.
def band_oz(key):
    if key == "sub":
        return SUB_POUND_OZ
    return int(key) * 16


def band_label(key):
    return "Under 1 lb" if key == "sub" else f"{key} lb"


# ---------- input --------------------------------------------------------------------------------
def parse_quotes(args):
    quotes = {}
    for a in args:
        if "=" not in a:
            raise SystemExit(f"Not a quote: {a!r}. Use band=price, e.g. 4=11.40 or sub=5.75")
        k, v = a.split("=", 1)
        quotes[k.strip()] = v.strip()
    return quotes


def normalise(raw):
    """Validate keys and money, and return {key: cents} — refusing anything that cannot be a rate."""
    out = {}
    for key, value in raw.items():
        k = str(key).strip().lower()
        if k not in ("sub", "over") and not (k.isdigit() and 1 <= int(k) <= MAX_POUNDS):
            raise SystemExit(f"Unknown band {key!r}. Use sub, 1..{MAX_POUNDS}, or over.")
        text = str(value).strip().lstrip("$")
        try:
            dollars = float(text)
        except ValueError:
            raise SystemExit(f"{band_label(k)}: {value!r} is not a price.")
        if dollars <= 0:
            raise SystemExit(f"{band_label(k)}: a rate must be more than zero.")
        if dollars > 200:
            raise SystemExit(f"{band_label(k)}: ${dollars} looks like a typo, not Ground Advantage.")
        cents = round(dollars * 100)
        out[k] = cents
    return out


def check_shape(cents):
    """Everything that can be known without the carrier: ordering, gaps, and obvious typos."""
    problems, notes = [], []
    pounds = sorted(int(k) for k in cents if k not in ("sub", "over"))

    ladder = ([("sub", cents["sub"])] if "sub" in cents else []) + [(str(p), cents[str(p)]) for p in pounds]
    for i in range(1, len(ladder)):
        (pk, pv), (k, v) = ladder[i - 1], ladder[i]
        if v <= pv:
            problems.append(f"{band_label(k)} (${v/100:.2f}) is not dearer than "
                            f"{band_label(pk)} (${pv/100:.2f}) — a heavier parcel must cost more.")

    if "over" in cents and ladder:
        top_k, top_v = ladder[-1]
        if cents["over"] <= top_v:
            problems.append(f"The over-max fallback (${cents['over']/100:.2f}) is not dearer than "
                            f"{band_label(top_k)} (${top_v/100:.2f}). It covers unbounded weight.")

    # A decimal slip usually shows up as a step an order of magnitude off its neighbours.
    steps = [ladder[i][1] - ladder[i - 1][1] for i in range(1, len(ladder))]
    if len(steps) >= 3:
        typical = sorted(steps)[len(steps) // 2]
        for i, st in enumerate(steps, start=1):
            if typical > 0 and (st > typical * 8 or st * 8 < typical):
                notes.append(f"{band_label(ladder[i][0])} jumps ${st/100:.2f} where the usual step "
                             f"is about ${typical/100:.2f} — worth re-reading the quote.")

    if pounds:
        missing = [p for p in range(1, max(pounds) + 1) if p not in pounds]
        if missing:
            notes.append("Pounds with no band: " + ", ".join(map(str, missing)) +
                         " — each pays the band above. KNOWN_MISSING_POUNDS is set to match.")
        if max(pounds) < MAX_POUNDS:
            notes.append(f"Nothing quoted above {max(pounds)} lb, so a {max(pounds)+1}–{MAX_POUNDS} lb "
                         f"parcel pays the flat fallback. Six pairs of shoes is {MAX_POUNDS} lb.")
    return problems, notes


# ---------- rendering ----------------------------------------------------------------------------
def ordered(cents):
    keys = ([("sub", SUB_POUND_OZ)] if "sub" in cents else [])
    keys += [(str(p), p * 16) for p in sorted(int(k) for k in cents if k not in ("sub", "over"))]
    return keys


def render_js(cents):
    rows = []
    for key, oz in ordered(cents):
        price = cents[key] / 100
        text = f"{price:.2f}".rstrip("0").rstrip(".")
        rows.append(f'  {{ maxOz: {oz}, price: {text}, label: "{band_label(key)}" }},')
    return "const SHIPPING_TIERS = [\n" + "\n".join(rows) + "\n];"


def render_py(cents):
    pairs = [f"({oz}, {cents[key]})" for key, oz in ordered(cents)]
    one = "SHIPPING_TIERS = [" + ", ".join(pairs) + "]"
    if len(one) <= 96:
        return one
    return "SHIPPING_TIERS = [\n    " + ",\n    ".join(pairs) + ",\n]"


def render_missing(cents):
    pounds = sorted(int(k) for k in cents if k not in ("sub", "over"))
    if not pounds:
        return "KNOWN_MISSING_POUNDS = set()"
    missing = [p for p in range(1, max(pounds) + 1) if p not in pounds]
    return "KNOWN_MISSING_POUNDS = " + ("{" + ", ".join(map(str, missing)) + "}" if missing else "set()")


# ---------- editing ------------------------------------------------------------------------------
def sub(path, pattern, replacement, what):
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    new, n = re.subn(pattern, lambda m: replacement, src, count=1, flags=re.S)
    if n != 1:
        raise SystemExit(f"Could not find {what} in {os.path.relpath(path, APP_DIR)} — "
                         "has the block been renamed or reformatted?")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new)


def apply(cents):
    sub(JS, r"const SHIPPING_TIERS = \[.*?\];", render_js(cents), "SHIPPING_TIERS")
    if "over" in cents:
        sub(JS, r"const SHIPPING_OVER_MAX = [0-9.]+;",
            f"const SHIPPING_OVER_MAX = {cents['over'] / 100:.2f}".rstrip("0").rstrip(".") + ";",
            "SHIPPING_OVER_MAX")
    sub(PY, r"SHIPPING_TIERS = \[.*?\]\n", render_py(cents) + "\n", "SHIPPING_TIERS")
    if "over" in cents:
        sub(PY, r"SHIPPING_OVER_MAX = \d+", f"SHIPPING_OVER_MAX = {cents['over']}", "SHIPPING_OVER_MAX")
    sub(TEST, r"KNOWN_MISSING_POUNDS = (?:\{[^}]*\}|set\(\))", render_missing(cents),
        "KNOWN_MISSING_POUNDS")


def run_tests():
    for suite in SUITES:
        r = subprocess.run([sys.executable, suite], capture_output=True, text=True)
        if r.returncode != 0:
            return os.path.relpath(suite, APP_DIR), (r.stderr or r.stdout)[-2500:]
    return None, None


# ---------- main ---------------------------------------------------------------------------------
def show_current():
    with open(PY, encoding="utf-8") as fh:
        src = fh.read()
    tiers = re.search(r"SHIPPING_TIERS = \[(.*?)\]\n", src, re.S)
    over = re.search(r"SHIPPING_OVER_MAX = (\d+)", src)
    print("Current ladder (from db/orders.py, the figure actually charged):")
    for oz, c in re.findall(r"\(([0-9.]+), (\d+)\)", tiers.group(1)):
        lb = "under 1" if float(oz) == SUB_POUND_OZ else f"{round(float(oz) / 16)}"
        print(f"  {lb:>8} lb   {oz:>6} oz   ${int(c)/100:>6.2f}")
    print(f"  {'over':>8}      —      ${int(over.group(1))/100:>6.2f}")


def main():
    args = [a for a in sys.argv[1:]]
    if "--show" in args:
        show_current()
        return 0

    dry = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]

    if "--quotes" in args:
        i = args.index("--quotes")
        with open(args[i + 1], encoding="utf-8") as fh:
            raw = json.load(fh)
        raw = {k: v for k, v in raw.items() if not str(k).startswith("_")}
    else:
        raw = parse_quotes(args)
    if not raw:
        print(__doc__)
        return 2

    cents = normalise(raw)
    problems, notes = check_shape(cents)

    print("Ladder to apply:")
    for key, oz in ordered(cents):
        print(f"  {band_label(key):>11}   {oz:>6} oz   ${cents[key]/100:>6.2f}")
    if "over" in cents:
        print(f"  {'over the top':>11}        —   ${cents['over']/100:>6.2f}")
    print()

    for n in notes:
        print(f"  note: {n}")
    for p in problems:
        print(f"  PROBLEM: {p}")
    if problems:
        print("\nNothing written. Fix the quotes above and run again.")
        return 1
    if notes:
        print()

    if dry:
        print("--- script.js ---\n" + render_js(cents))
        print("\n--- db/orders.py ---\n" + render_py(cents))
        print("\n--- tests/test_shipping_weights.py ---\n" + render_missing(cents))
        print("\nDry run: nothing written.")
        return 0

    backup = tempfile.mkdtemp(prefix="poc-rates-")
    for f in (JS, PY, TEST):
        shutil.copy2(f, os.path.join(backup, os.path.basename(f)))
    try:
        apply(cents)
        suite, output = run_tests()
        if suite:
            for f in (JS, PY, TEST):
                shutil.copy2(os.path.join(backup, os.path.basename(f)), f)
            print(f"TESTS FAILED in {suite} — every file restored, nothing changed.\n")
            print(output)
            return 1
    except Exception:
        for f in (JS, PY, TEST):
            shutil.copy2(os.path.join(backup, os.path.basename(f)), f)
        raise
    finally:
        shutil.rmtree(backup, ignore_errors=True)

    print("Applied to script.js, db/orders.py and tests/test_shipping_weights.py. Both suites pass.")
    print("Still to do by hand: bump ?v= on script.js and styles.css in index.html, then commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
