"""Shipping configuration: weights AND the rate table, checked for validity and for parity.

Two separate properties, and the distinction is the whole point of this file.

  PARITY   — script.js and db/orders.py agree. The existing test_pricing_parity covers this.
  VALIDITY — those keys match the categories in products.json.

Only parity was ever checked, and that is exactly why the bug survived: the rename in ff30d32 made
the storefront's categories `Shirts` and `Bags` while both weight tables still said `T-Shirts` and
`Backpacks`. The two files agreed perfectly with each other and were both wrong, so every shirt and
bag fell through to the 8 oz default — a rounding error on a shirt, and a 24 oz undercharge on
every bag sold. A missing key looks identical to "no estimate entered yet", so nothing complained.

The rate table gets the same treatment. Its band edges must land on whole pounds, because USPS
bills a parcel at its rounded-up pound — an edge at, say, 70 oz would charge two different prices
for parcels the carrier treats identically. Which pounds have a band at all is tracked separately
in KNOWN_MISSING_POUNDS below, so the current gap is recorded as a fact rather than a surprise.

    python3 tests/test_shipping_weights.py
"""
import json
import os
import re
import types
import unittest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Pounds that have no band of their own, so a parcel of that weight pays the next band up. This is
# a RECORD OF A KNOWN GAP, not an approval of it: the table jumps 3 -> 5 -> 10, so a 6 lb order is
# charged the 10 lb rate. See the worksheet in STRIPE.md.
#
# **Shrink this set as bands are added, and delete it when it reaches empty.** The test below
# compares it against reality, so adding a 6 lb band without updating this fails loudly rather than
# passing quietly — which is the point: the gap should never widen again unnoticed, and closing it
# should be a deliberate edit rather than something nobody records.
KNOWN_MISSING_POUNDS = {4, 6, 7, 8, 9}

# The sub-1-lb band is deliberately not on a pound boundary: everything under a pound is one flat
# price, so its edge sits just below 16 oz.
SUB_POUND_EDGE_OZ = 15.99


def load_orders():
    """Execute db/orders.py from source, every time.

    NOT spec_from_file_location + exec_module: that honours __pycache__, and a .pyc is validated on
    (mtime, size). An edit that keeps the byte length and lands in the same second as the cached
    build — exactly what a one-figure change to the rate table looks like — is served from the
    cache, so the tests would check bytecode instead of the file on disk. This file's entire job is
    asserting things about that source, so it reads it directly.
    """
    path = os.path.join(APP_DIR, "db", "orders.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    mod = types.ModuleType("_orders_under_test")
    mod.__file__ = path
    exec(compile(src, path, "exec"), mod.__dict__)
    return mod


def js_weight_map():
    """Read CATEGORY_WEIGHT_OZ straight out of script.js.

    Parsed rather than duplicated here: a copy in the test would drift the same way the two source
    copies did, and then agree with nothing.
    """
    with open(os.path.join(APP_DIR, "script.js"), encoding="utf-8") as fh:
        src = fh.read()
    m = re.search(r"const CATEGORY_WEIGHT_OZ = \{(.*?)\};", src, re.S)
    assert m, "CATEGORY_WEIGHT_OZ not found in script.js — did the block move or get renamed?"
    out = {}
    for line in m.group(1).splitlines():
        line = line.split("//")[0].strip()          # drop comments
        if not line or line == "}":
            continue
        entry = re.match(r'^"?([A-Za-z -]+)"?\s*:\s*([0-9.]+)\s*,?$', line)
        if entry:
            out[entry.group(1)] = float(entry.group(2))
    assert out, "parsed no weights out of script.js"
    return out


def js_rate_table():
    """SHIPPING_TIERS, SHIPPING_OVER_MAX and PACKAGING_OZ out of script.js, in cents.

    Parsed from source for the same reason as the weights: a copy kept here would drift alongside
    the thing it is supposed to be checking.
    """
    with open(os.path.join(APP_DIR, "script.js"), encoding="utf-8") as fh:
        src = fh.read()

    block = re.search(r"const SHIPPING_TIERS = \[(.*?)\];", src, re.S)
    assert block, "SHIPPING_TIERS not found in script.js"
    tiers = []
    for max_oz, price in re.findall(r"maxOz:\s*([0-9.]+)\s*,\s*price:\s*([0-9.]+)", block.group(1)):
        tiers.append((float(max_oz), round(float(price) * 100)))
    assert tiers, "parsed no tiers out of script.js"

    over = re.search(r"const SHIPPING_OVER_MAX = ([0-9.]+)", src)
    pack = re.search(r"const PACKAGING_OZ = ([0-9.]+)", src)
    assert over and pack, "SHIPPING_OVER_MAX or PACKAGING_OZ not found in script.js"
    return tiers, round(float(over.group(1)) * 100), float(pack.group(1))


def catalogue():
    with open(os.path.join(APP_DIR, "products.json"), encoding="utf-8") as fh:
        return json.load(fh)


class TestShippingWeights(unittest.TestCase):
    def setUp(self):
        self.js = js_weight_map()
        self.py = load_orders().CATEGORY_WEIGHT_OZ
        self.doc = catalogue()
        # "All" is the filter tile that means no filter, not a real category to ship.
        self.declared = [c for c in self.doc.get("categories", []) if c != "All"]
        self.in_use = sorted({p["category"] for p in self.doc["products"]})

    def test_every_category_with_stock_has_a_weight(self):
        """The check that was missing. A category with no entry bills at the fallback, silently."""
        missing = [c for c in self.in_use if c not in self.py]
        self.assertEqual(missing, [], f"categories with stock and no shipping weight: {missing}")

    def test_every_declared_category_has_a_weight(self):
        """Including empty ones — a tile is seeded before its first product, and that product
        must not be the thing that discovers the hole."""
        missing = [c for c in self.declared if c not in self.py]
        self.assertEqual(missing, [], f"declared categories with no shipping weight: {missing}")

    def test_no_weight_key_is_a_dead_category(self):
        """A key for a category that no longer exists is how the last bug looked from the inside."""
        dead = [k for k in self.py if k not in self.declared]
        self.assertEqual(dead, [], f"weight keys matching no category: {dead}")

    def test_js_and_python_agree(self):
        """The site quotes from script.js; the card is charged from db/orders.py. They must match."""
        self.assertEqual(self.js, self.py,
                         "script.js and db/orders.py disagree about shipping weights")

    def test_weights_are_positive_numbers(self):
        for cat, oz in self.py.items():
            self.assertIsInstance(oz, (int, float), cat)
            self.assertGreater(oz, 0, cat)

    def test_bags_are_not_billed_as_light_goods(self):
        """The specific regression: a bag at the 8 oz default is a 24 oz undercharge every time."""
        orders = load_orders()
        self.assertIn("Bags", self.py)
        self.assertGreater(self.py["Bags"], orders.DEFAULT_WEIGHT_OZ,
                           "a bag must not weigh the same as the unknown-category fallback")


class TestRateTable(unittest.TestCase):
    """The rate table itself: same two properties as the weights — valid, and the same on both sides."""

    def setUp(self):
        self.js_tiers, self.js_over, self.js_pack = js_rate_table()
        orders = load_orders()
        self.py_tiers = [(float(oz), int(cents)) for oz, cents in orders.SHIPPING_TIERS]
        self.py_over = orders.SHIPPING_OVER_MAX
        self.py_pack = float(orders.PACKAGING_OZ)

    # ---- parity ---------------------------------------------------------------------------------
    def test_js_and_python_rate_tables_agree(self):
        """script.js quotes the buyer, db/orders.py charges the card. A gap between them is a
        customer being shown one price and billed another."""
        self.assertEqual(self.js_tiers, self.py_tiers)
        self.assertEqual(self.js_over, self.py_over, "SHIPPING_OVER_MAX differs")
        self.assertEqual(self.js_pack, self.py_pack, "PACKAGING_OZ differs")

    # ---- validity -------------------------------------------------------------------------------
    def test_every_band_edge_is_a_whole_pound(self):
        """USPS bills at the rounded-up pound, so an edge anywhere else splits one carrier price
        into two of ours — two parcels the carrier treats identically, quoted differently."""
        for oz, _cents in self.py_tiers:
            if oz == SUB_POUND_EDGE_OZ:
                continue
            self.assertAlmostEqual(oz % 16, 0, places=6,
                                   msg=f"band edge {oz} oz is {oz / 16:.2f} lb, not a whole pound")

    def test_bands_ascend_by_weight_and_price(self):
        """Cheapest-first is what shippingFor() relies on: it returns the FIRST band the parcel
        fits under, so an out-of-order row would quietly undercharge every heavier parcel."""
        prev_oz = prev_cents = 0
        for oz, cents in self.py_tiers:
            self.assertGreater(oz, prev_oz, "band edges must strictly ascend")
            self.assertGreater(cents, prev_cents, "a heavier band must not be cheaper")
            prev_oz, prev_cents = oz, cents

    def test_over_max_costs_more_than_the_heaviest_band(self):
        self.assertGreater(self.py_over, self.py_tiers[-1][1],
                           "anything over the last band must not be cheaper than the last band")

    def test_price_never_falls_as_weight_rises(self):
        """The invariant a buyer would notice: adding an item must never make postage cheaper."""
        def quote(oz):
            for max_oz, cents in self.py_tiers:
                if oz <= max_oz:
                    return cents
            return self.py_over

        last = 0
        for oz in range(1, int(self.py_tiers[-1][0]) + 40):
            here = quote(oz)
            self.assertGreaterEqual(here, last, f"postage drops at {oz} oz")
            last = here

    # ---- coverage -------------------------------------------------------------------------------
    def test_band_coverage_matches_the_known_gap(self):
        """Which pounds have a band of their own, checked against the recorded gap.

        Fails in both directions on purpose. A band disappearing is a regression; a band being
        added is good news that must be written down, by shrinking KNOWN_MISSING_POUNDS — and when
        that set is empty, this becomes a plain "every pound is covered" test.
        """
        covered = {round(oz / 16) for oz, _ in self.py_tiers if oz != SUB_POUND_EDGE_OZ}
        heaviest = max(covered)
        missing = {lb for lb in range(1, heaviest + 1) if lb not in covered}
        self.assertEqual(
            missing, KNOWN_MISSING_POUNDS,
            f"band coverage changed: pounds with no band are now {sorted(missing)}, "
            f"recorded as {sorted(KNOWN_MISSING_POUNDS)}. If bands were added, shrink "
            f"KNOWN_MISSING_POUNDS in this file to match (and delete it once it is empty).")

    def test_the_gap_is_only_ever_an_overcharge(self):
        """A parcel in a missing band pays the band above, never one below — so the shop is not the
        one losing money on the gap. If this ever fails, the table has been reordered."""
        def quote(oz):
            for max_oz, cents in self.py_tiers:
                if oz <= max_oz:
                    return cents
            return self.py_over

        for lb in sorted(KNOWN_MISSING_POUNDS):
            oz = lb * 16
            charged = quote(oz)
            lighter = quote((lb - 1) * 16)
            self.assertGreaterEqual(charged, lighter,
                                    f"{lb} lb is charged less than {lb - 1} lb")


if __name__ == "__main__":
    unittest.main(verbosity=2)
