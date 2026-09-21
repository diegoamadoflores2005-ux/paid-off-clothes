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
import argparse
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

    def test_the_fallback_weight_agrees_across_the_two_files(self):
        """The default applies to any category with no entry, so a drift here is silent by design."""
        import re
        orders = load_orders()
        src = open(os.path.join(APP_DIR, "script.js"), encoding="utf-8").read()
        m = re.search(r"const DEFAULT_WEIGHT_OZ = ([0-9.]+)", src)
        self.assertIsNotNone(m, "DEFAULT_WEIGHT_OZ not found in script.js — inline it and this "
                                "check goes blind, which is how the bag weight drifted before")
        self.assertEqual(float(m.group(1)), float(orders.DEFAULT_WEIGHT_OZ))

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


class TestZonePricing(unittest.TestCase):
    """Zone-aware shipping, exercised against a synthetic table.

    The real shipping_rates.json is empty on purpose — no quote has been collected yet — so these
    build a filled one in a temp file and point the module at it. That tests the machinery without
    inventing a rate anyone might mistake for a carrier quote.
    """

    def _with_rates(self, rates):
        import json
        import tempfile
        orders = load_orders()
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(rates, fh)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        orders._RATES_PATH = fh.name
        orders._rates_cache["mtime"] = None
        return orders

    SERVICE = "Ground Advantage"
    BASIS = "weight"

    def _full_table(self):
        bands = ["sub"] + [str(b) for b in range(2, 10)]
        rates = {
            "origin_zip": "85641",
            "zone_policy": "banded",
            "carrier": "USPS",
            "service": self.SERVICE,
            "rate_basis": self.BASIS,
            "zone_groups": {"near": {"zones": [1, 2, 3, 4]},
                            "mid": {"zones": [5, 6]},
                            "far": {"zones": [7, 8, 9]}},
            "zone_map": {"902": 4, "800": 5, "100": 8},
            "max_quotable_oz": 9 * 16,
            "packaging": {"boxes": [{"name": "fixture", "dims_in": [12, 12, 11],
                                     "cu_in": 1584, "verified": True}]},
            "rate_table": {
                "core": {b: {"near": 5 + i, "mid": 6 + i, "far": 7 + i}
                         for i, b in enumerate(bands)},
                "heavy": {},
                "over_max": {"near": 30, "mid": 33, "far": 36},
            },
        }
        rates["cell_provenance"] = self._provenance_for(rates)
        return rates

    def _provenance_for(self, rates):
        """Mark every filled required cell as a verified quote on the table's own service.

        The fixture has to supply this now: a price with no provenance is treated as unverified, so
        without it a fully-filled synthetic table would never switch on.
        """
        orders = load_orders()
        return {f"{section}.{band}.{group}": {"verified": True,
                                              "service": rates["service"],
                                              "rate_basis": rates["rate_basis"]}
                for (section, band, group), price in orders.required_cells(rates)
                if price is not None}

    # ---- the gate ------------------------------------------------------------------------------
    def test_the_real_table_is_not_ready_yet(self):
        """Nothing has been quoted, so the live site must still be on the flat ladder."""
        orders = load_orders()
        self.assertFalse(orders.zone_pricing_ready(),
                         "zone pricing must stay off until every required cell holds a quote")

    # ---- the real file -------------------------------------------------------------------------
    # These run against shipping_rates.json itself, not a fixture. Every quote collected from here
    # on lands in that file, and a typo or a misread service line is exactly the kind of thing that
    # looks fine in a diff. These are the checks that would have caught the ones already found.
    def test_the_real_table_is_sound(self):
        """Whatever is in the file must obey the arithmetic of postage, at every stage of filling.

        This is the check that rejects a Cubic price sitting in a weight band, a column filled in
        the wrong order, and the $9.15 that outpriced a heavier parcel.
        """
        orders = load_orders()
        self.assertEqual(orders.rate_table_problems(), [],
                         "the real rate table is holding a combination that cannot be right")

    def test_every_verified_cell_says_how_it_was_verified(self):
        """A cell marked verified with no evidence is a claim, not a record."""
        orders = load_orders()
        rates = orders.load_rates()
        for cell, entry in (rates.get("cell_provenance") or {}).items():
            if not entry.get("verified"):
                continue
            with self.subTest(cell=cell):
                self.assertTrue(entry.get("evidence"), f"{cell} is verified but says nothing about "
                                                       f"how — that is unfalsifiable")
                self.assertIn("service", entry, f"{cell} does not name the service it was quoted on")
                self.assertIn("rate_basis", entry, f"{cell} does not name its rate basis")

    def test_no_cell_holds_a_price_from_another_carrier(self):
        """The very first conflict in this file was a UPS price filed as a USPS rate."""
        orders = load_orders()
        rates = orders.load_rates()
        allowed = orders.allowed_services(rates)
        for cell, entry in (rates.get("cell_provenance") or {}).items():
            with self.subTest(cell=cell):
                self.assertIn(entry.get("service"), allowed,
                              f"{cell} is filled from a service this table cannot hold")

    def test_the_verified_near_ladder_matches_what_was_quoted(self):
        """The five confirmed near cells, as collected. A silent edit to any of them fails here."""
        orders = load_orders()
        table = orders.load_rates()["rate_table"]["core"]
        self.assertEqual(
            {b: table[b]["near"] for b in ("sub", "2", "3", "4", "5")},
            {"sub": 5.83, "2": 6.03, "3": 6.33, "4": 6.40, "5": 7.03})
        self.assertIn("1", table, "the 1 lb band must exist so 16.0 oz does not round into 2 lb")
        self.assertIsNone(table["1"]["near"], "no 1 lb rate has been quoted; none may be invented")

    def test_a_group_quoted_below_its_worst_zone_is_rejected(self):
        """One price per group must cover the group's dearest zone.

        near spans zones 1-4 and was quoted at 90210, which is zone 4 — its worst case. Quoting it
        at zone 3 instead would ship every zone 4 order below cost, on every order, with nothing on
        the page or in the order to say which zone it went to.
        """
        rates = self._full_table()
        orders = self._with_rates(rates)
        self.assertEqual(orders.worst_case_zone("near", rates), 4)
        self.assertEqual(orders.worst_case_zone("far", rates), 8)

        rates["cell_provenance"]["core.3.near"]["dest_zip"] = "85701"   # zone 1 in this fixture
        rates["zone_map"]["857"] = 1
        orders = self._with_rates(rates)
        self.assertTrue(any(cell == "core.3.near" for cell, _w in orders.unverified_cells(rates)))
        self.assertFalse(orders.zone_pricing_ready())

    def test_zone_nine_has_its_own_group(self):
        """Prefix 969 used to sit inside `far`, alongside zones 7 and 8.

        One price per group means that group must be quoted at its dearest zone, so zone 9 inside
        `far` forced a choice between charging the entire east coast a Pacific-territory rate and
        shipping every 969 order below cost. Splitting it lets each carry its own verified rate.
        """
        orders = load_orders()
        rates = orders.load_rates()
        self.assertEqual(rates["zone_groups"]["far"]["zones"], [7, 8])
        self.assertEqual(rates["zone_groups"]["territories"]["zones"], [9])
        self.assertEqual(orders.worst_case_zone("far"), 8,
                         "far must now be quotable at zone 8, its real worst case")
        self.assertEqual(orders.worst_case_zone("territories"), 9)

    def test_prefix_969_routes_to_territories(self):
        orders = load_orders()
        self.assertEqual(orders.zone_for_zip("96910"), 9, "Guam")
        self.assertEqual(orders.group_for_zone(9), "territories")
        self.assertEqual(orders.group_for_zone(8), "far")

    def test_the_new_group_is_required_before_anything_switches_on(self):
        """An incomplete group may not go live on its own — the gate stays all-or-nothing."""
        orders = load_orders()
        required = {g for (_sec, _band, g), _p in orders.required_cells()}
        self.assertIn("territories", required,
                      "territories cells must be required, or the table could switch on without them")
        table = orders.load_rates()["rate_table"]["core"]
        self.assertTrue(all(row.get("territories") is None for row in table.values()),
                        "no territories rate has been quoted; none may be invented")
        self.assertFalse(orders.zone_pricing_ready())

    def test_neighbouring_prefixes_can_be_in_different_groups(self):
        """The trap that cost a quoting session: a city name is not a zone.

        Birmingham AL (352) and Tuscaloosa AL (354) are an hour apart and fall either side of the
        mid/far line. Reading "354, 355, 356" off a list and substituting a city you recognise is
        how a whole column gets quoted to the wrong zone, and the quoting is done before anyone
        notices. Pirate Ship reporting zone 7 for 35203 agreed with the map exactly.
        """
        orders = load_orders()
        self.assertEqual(orders.zone_for_zip("35203"), 7, "Birmingham — far, not mid")
        self.assertEqual(orders.group_for_zone(orders.zone_for_zip("35203")), "far")
        self.assertEqual(orders.zone_for_zip("35401"), 6, "Tuscaloosa — mid")
        self.assertEqual(orders.group_for_zone(orders.zone_for_zip("35401")), "mid")

    def test_groups_are_ordered_nearest_first(self):
        """rate_table_problems() walks them outward to check postage never falls with distance."""
        orders = load_orders()
        self.assertEqual(orders.group_names(), ("near", "mid", "far", "territories"))

    def test_the_group_list_is_never_hardcoded(self):
        """Four files used to carry the three group names as a literal tuple.

        That is the same shape as the category-weight bug: adding a group leaves copies quietly
        pricing three while the data describes four, and a missing key reads exactly like "no rate
        yet". Everything now derives from zone_groups.
        """
        literal = '"near", "mid", "far"'
        for rel in ("db/orders.py", "tools/shipping_gaps.py", "tools/record_quote.py"):
            with self.subTest(file=rel):
                src = open(os.path.join(APP_DIR, rel), encoding="utf-8").read()
                self.assertNotIn(literal, src,
                                 f"{rel} hardcodes the group list; derive it from zone_groups")

    def test_a_storage_container_is_never_the_shipping_package(self):
        """The inventory box is recorded for reference and must stay out of the rate path.

        `containers` is reference data; `quoted_for_package` is the only thing the rate code reads.
        Cubic is priced on the package, so a container promoted into that slot by a careless edit
        would reprice every heavy order against a box nothing ships in. This box would not even be
        Cubic-eligible — 1.84 cu ft against a 1.0 cu ft cap — so the wrongness would be silent
        rather than loud.
        """
        orders = load_orders()
        rates = orders.load_rates()
        inventory = (rates.get("containers") or {}).get("inventory_box") or {}
        self.assertFalse(inventory.get("may_unlock_cubic_rates", False))
        shipping = (rates.get("quoted_for_package") or {}).get("dims_in")
        self.assertNotEqual(shipping, inventory.get("dims_in"),
                            "the storage container has been set as the shipping package")

    def test_the_cubic_bands_are_still_empty(self):
        """6-9 lb quoted $8.56 on Cubic, which bills on volume. Until the mailer is measured those
        cells must stay empty — a volume price for an unmeasured box is not a price."""
        orders = load_orders()
        table = orders.load_rates()["rate_table"]["core"]
        for band in ("6", "7", "8", "9"):
            self.assertIsNone(table[band]["near"],
                              f"{band} lb was filled from a Cubic quote without a measured box")

    def test_an_empty_zone_map_keeps_zone_pricing_off(self):
        rates = self._full_table()
        rates["zone_map"] = {}
        orders = self._with_rates(rates)
        self.assertFalse(orders.zone_pricing_ready())
        self.assertIsNone(orders.zone_shipping_cents(38, "90210"))

    def test_one_missing_cell_keeps_zone_pricing_off(self):
        """All or nothing: a partly filled table would price two identical baskets by different
        rules depending on which cell happened to be filled."""
        rates = self._full_table()
        rates["rate_table"]["core"]["5"]["mid"] = None
        orders = self._with_rates(rates)
        self.assertFalse(orders.zone_pricing_ready())

    def test_a_complete_table_switches_zone_pricing_on(self):
        orders = self._with_rates(self._full_table())
        self.assertTrue(orders.zone_pricing_ready())

    # ---- the mapping ---------------------------------------------------------------------------
    def test_zone_comes_from_the_map_never_from_distance(self):
        orders = self._with_rates(self._full_table())
        self.assertEqual(orders.zone_for_zip("90210"), 4)
        self.assertEqual(orders.zone_for_zip("90210-1234"), 4, "ZIP+4 must resolve on the prefix")
        self.assertIsNone(orders.zone_for_zip("59718"), "an unmapped prefix is unknown, not guessed")
        self.assertIsNone(orders.zone_for_zip("9"), "too short to carry a prefix")
        self.assertIsNone(orders.zone_for_zip(""))

    def test_groups_resolve_from_zones(self):
        orders = self._with_rates(self._full_table())
        self.assertEqual(orders.group_for_zone(4), "near")
        self.assertEqual(orders.group_for_zone(5), "mid")
        self.assertEqual(orders.group_for_zone(8), "far")
        self.assertIsNone(orders.group_for_zone(None))

    # ---- pricing -------------------------------------------------------------------------------
    def test_the_same_basket_costs_more_further_away(self):
        """The whole point of zone pricing."""
        orders = self._with_rates(self._full_table())
        near = orders.zone_shipping_cents(38, "90210")     # zone 4
        mid = orders.zone_shipping_cents(38, "80001")      # prefix 800 -> zone 5
        far = orders.zone_shipping_cents(38, "10001")      # zone 8
        self.assertLess(near, mid)
        self.assertLess(mid, far)

    def test_an_unmapped_destination_falls_back_rather_than_guessing(self):
        orders = self._with_rates(self._full_table())
        self.assertIsNone(orders.zone_shipping_cents(38, "59718"),
                          "an unknown prefix must not be priced at a neighbouring zone")

    def test_band_is_the_rounded_up_pound(self):
        orders = load_orders()
        self.assertEqual(orders.band_for_oz(10), "sub")
        self.assertEqual(orders.band_for_oz(15.99), "sub")
        self.assertEqual(orders.band_for_oz(17), "2")
        self.assertEqual(orders.band_for_oz(38), "3")
        self.assertEqual(orders.band_for_oz(143), "9", "20 shirts is a 9 lb parcel")

    def test_a_half_pound_bills_at_the_pound_above_it(self):
        """Checked against the carrier, not just the documentation.

        3.5 lb on 85641 -> 90210 quoted $6.40, the 4 lb rate, rather than $6.33 at 3 lb. Everything
        in the band model leans on this: band_for_oz rounds up, the table is indexed on whole
        pounds, and an unquoted pound charges the next band up. If billing rounded down or prorated
        instead, all three would be wrong.
        """
        orders = load_orders()
        self.assertEqual(orders.band_for_oz(3.5 * 16), "4")
        self.assertEqual(orders.band_for_oz(49), "4", "one ounce over 3 lb is already a 4 lb parcel")
        self.assertEqual(orders.band_for_oz(64), "4", "exactly 4 lb stays in the 4 lb band")

    def test_above_the_ceiling_the_table_refuses_to_price(self):
        """It used to fall to over_max — one flat price for everything heavier than the table.

        That is an UNDERcharge waiting to happen: a parcel heavier than the weight that price was
        quoted for ships below cost, and undercharges come out of the shop silently. Above the
        ceiling the table now says nothing and the order falls back to the estimate.
        """
        orders = self._with_rates(self._full_table())
        self.assertEqual(orders.max_quotable_oz(), 144)
        self.assertIsNone(orders.zone_shipping_cents(400, "90210"))
        self.assertIsNotNone(orders.zone_shipping_cents(143, "90210"), "under the ceiling still prices")

    def test_no_zip_means_the_flat_ladder(self):
        """Every cart. There is no address at that point, so there is no zone to price by."""
        orders = self._with_rates(self._full_table())
        self.assertEqual(orders.shipping_source(None), "estimate")
        self.assertEqual(orders.shipping_source("90210"), "zone")
        self.assertEqual(orders.shipping_source("59718"), "estimate",
                         "an unmapped destination is still an estimate")

    # ---- unquoted pounds -----------------------------------------------------------------------
    def test_an_unquoted_pound_pays_the_next_band_up_not_the_fallback(self):
        """1, 10, 12, 14 and 15 lb have no row. They must round up into the table, not out of it.

        The lookup used to drop any unquoted pound straight to over_max — the fallback for parcels
        heavier than the whole table, and the dearest cell in it. That inverted the ladder: a 10 lb
        order paid the over-max price while an 11 lb order paid the 11 lb rate.
        """
        rates = self._full_table()
        rates["rate_table"]["heavy"] = {"11": {"near": 20, "mid": 21, "far": 22}}
        rates["cell_provenance"] = self._provenance_for(rates)
        orders = self._with_rates(rates)

        rates["max_quotable_oz"] = 20 * 16
        orders = self._with_rates(rates)
        ten_lb = orders.zone_shipping_cents(160, "90210")
        eleven_lb = orders.zone_shipping_cents(176, "90210")
        self.assertEqual(ten_lb, 2000, "10 lb must pay the next band quoted above it, the 11 lb one")
        self.assertEqual(ten_lb, eleven_lb)

    def test_exactly_one_pound_is_not_charged_the_over_max_fallback(self):
        """16.0 oz rounds to the 1 lb band, which no table has. It must fall up to 2 lb."""
        orders = self._with_rates(self._full_table())
        self.assertEqual(orders.zone_shipping_cents(16, "90210"),
                         orders.zone_shipping_cents(17, "90210"))

    # ---- soundness -----------------------------------------------------------------------------
    def test_a_cheaper_heavier_band_keeps_zone_pricing_off(self):
        """The shape that a cubic quote makes when it lands in a weight table.

        These are the figures actually collected: $9.15 for a 2 lb weight-based parcel, then $5.93
        for 3, 4 and 5 lb — one price across three weights, which is the signature of Ground
        Advantage CUBIC, a volume-priced product. Nothing in the code noticed at the time.
        """
        rates = self._full_table()
        rates["rate_table"]["core"]["2"]["near"] = 9.15
        for band in ("3", "4", "5"):
            rates["rate_table"]["core"][band]["near"] = 5.93
        orders = self._with_rates(rates)
        problems = orders.rate_table_problems(rates)
        self.assertTrue(problems, "a heavier band priced below a lighter one must be reported")
        self.assertIn("3", problems[0])
        self.assertFalse(orders.zone_pricing_ready(),
                         "an unsound table must never start charging customers")

    def test_a_cheaper_further_zone_keeps_zone_pricing_off(self):
        """far below near in one band means the columns were filled in the wrong order."""
        rates = self._full_table()
        rates["rate_table"]["core"]["4"]["far"] = 1.00
        orders = self._with_rates(rates)
        self.assertTrue(orders.rate_table_problems(rates))
        self.assertFalse(orders.zone_pricing_ready())

    def test_a_discounted_over_max_is_rejected(self):
        rates = self._full_table()
        rates["rate_table"]["over_max"]["near"] = 1.00
        orders = self._with_rates(rates)
        self.assertTrue(orders.rate_table_problems(rates))
        self.assertFalse(orders.zone_pricing_ready())

    def test_a_sound_table_reports_no_problems(self):
        orders = self._with_rates(self._full_table())
        self.assertEqual(orders.rate_table_problems(), [])

    # ---- provenance ----------------------------------------------------------------------------
    def test_a_cubic_quote_cannot_fill_a_weight_table(self):
        """Cubic is a real price for a real service — just not one this table can be indexed on.

        Cubic is charged on the box's volume and is flat across weight up to 20 lb, so a cubic
        figure in a weight-indexed ladder prices every other weight in that band wrongly.
        """
        rates = self._full_table()
        rates["cell_provenance"]["core.3.near"] = {
            "verified": True, "service": "Ground Advantage Cubic", "rate_basis": "volume"}
        orders = self._with_rates(rates)
        unverified = orders.unverified_cells(rates)
        self.assertTrue(any(cell == "core.3.near" for cell, _why in unverified))
        self.assertFalse(orders.zone_pricing_ready())

    def test_a_cell_with_no_provenance_keeps_zone_pricing_off(self):
        rates = self._full_table()
        del rates["cell_provenance"]["core.sub.near"]
        orders = self._with_rates(rates)
        self.assertFalse(orders.zone_pricing_ready())

    def test_a_cell_flagged_unverified_keeps_zone_pricing_off(self):
        rates = self._full_table()
        rates["cell_provenance"]["core.2.near"]["verified"] = False
        orders = self._with_rates(rates)
        self.assertFalse(orders.zone_pricing_ready())

    # ---- the rate-shopped table ----------------------------------------------------------------
    # Pirate Ship rate shops weight-based Ground Advantage against Ground Advantage Cubic and shows
    # whichever wins, so a shop that buys the cheapest line on the screen is not on one service. A
    # table can model that honestly, but only for a package it names: Cubic is charged on volume, so
    # the moment the box changes, half the ladder is for a different box.
    BOX = [12, 19, 3]

    def _shopped_table(self):
        rates = self._full_table()
        rates["rate_basis"] = "cheapest_available"
        rates["rate_shopped_services"] = ["Ground Advantage", "Ground Advantage Cubic"]
        rates["quoted_for_package"] = {"dims_in": self.BOX, "verified": True}
        for entry in rates["cell_provenance"].values():
            entry["rate_basis"] = "cheapest_available"
            entry["dims_in"] = self.BOX
        return rates

    def test_a_rate_shopped_table_may_hold_a_cubic_price(self):
        """The 6-9 lb rows really are Cubic. A table that says so is allowed to carry them."""
        rates = self._shopped_table()
        rates["cell_provenance"]["core.8.near"]["service"] = "Ground Advantage Cubic"
        orders = self._with_rates(rates)
        self.assertEqual(orders.unverified_cells(rates), [])
        self.assertTrue(orders.zone_pricing_ready())

    def test_a_rate_shopped_table_without_a_package_cannot_switch_on(self):
        rates = self._shopped_table()
        del rates["quoted_for_package"]
        orders = self._with_rates(rates)
        self.assertIn("quoted_for_package", orders.package_problem(rates))
        self.assertFalse(orders.zone_pricing_ready())

    def test_an_estimated_package_cannot_switch_on(self):
        """'I think the mailer is about 12x19x3' is not a measurement, and Cubic bills on it."""
        rates = self._shopped_table()
        rates["quoted_for_package"]["verified"] = False
        orders = self._with_rates(rates)
        self.assertIsNotNone(orders.package_problem(rates))
        self.assertFalse(orders.zone_pricing_ready())

    def test_a_cell_quoted_for_a_different_box_is_rejected(self):
        rates = self._shopped_table()
        rates["cell_provenance"]["core.5.near"]["dims_in"] = [12, 9, 3]
        orders = self._with_rates(rates)
        self.assertTrue(any(cell == "core.5.near" for cell, _w in orders.unverified_cells(rates)))
        self.assertFalse(orders.zone_pricing_ready())

    def test_a_weight_table_still_refuses_cubic(self):
        """The default basis is unchanged: one service, and Cubic is not it."""
        rates = self._full_table()
        rates["cell_provenance"]["core.4.near"]["service"] = "Ground Advantage Cubic"
        orders = self._with_rates(rates)
        self.assertFalse(orders.zone_pricing_ready())

    def test_a_flat_run_of_bands_is_allowed_once_cubic_caps_the_ladder(self):
        """Cubic is flat across weight, so a rate-shopped ladder legitimately plateaus.

        Monotonicity must stay a >= check, not a > one, or the correct shape gets rejected.
        """
        rates = self._shopped_table()
        # The cap has to sit at or above the last weight-based band — which is how the real numbers
        # came in, $8.56 of Cubic above $5.93 of weight-based. A plateau BELOW the band before it is
        # still a fault, and the guard says so.
        cap = rates["rate_table"]["core"]["5"]["near"] + 0.5
        for band in ("6", "7", "8", "9"):
            rates["rate_table"]["core"][band]["near"] = cap
            rates["cell_provenance"][f"core.{band}.near"]["service"] = "Ground Advantage Cubic"
        orders = self._with_rates(rates)
        self.assertEqual([p for p in orders.rate_table_problems(rates) if "near:" in p], [],
                         "a flat run is legitimate once Cubic caps the ladder")

        rates["rate_table"]["core"]["6"]["near"] = 1.00
        self.assertTrue([p for p in orders.rate_table_problems(rates) if "near:" in p],
                        "a plateau that DROPS below the band before it is still a fault")


class TestTheSwitchOn(unittest.TestCase):
    """What happens the day the table is finished — proven before it happens, not after.

    Everything else tests the table while it is OFF. This exercises the transition: with a complete,
    sound, fully-verified table, shipping_cents() must start pricing by zone, and the cart — which
    has no address — must keep using the flat ladder. Getting that wrong would mean two identical
    baskets priced by different rules, which is the failure the all-or-nothing gate exists to stop.
    """

    def _complete_rates(self):
        bands = ["sub", "1"] + [str(b) for b in range(2, 10)]
        rates = {
            "origin_zip": "85641", "carrier": "USPS", "service": "Ground Advantage",
            "rate_basis": "weight",
            "zone_groups": {"near": {"zones": [1, 2, 3, 4]}, "mid": {"zones": [5, 6]},
                            "far": {"zones": [7, 8, 9]}},
            "zone_map": {"902": 4, "800": 5, "100": 8},
            "max_quotable_oz": 9 * 16,
            "packaging": {"boxes": [{"name": "fixture", "dims_in": [12, 12, 11],
                                     "cu_in": 1584, "verified": True}]},
            "rate_table": {
                "core": {b: {"near": 5.0 + i, "mid": 6.0 + i, "far": 7.0 + i}
                         for i, b in enumerate(bands)},
                "heavy": {}, "over_max": {"near": 40.0, "mid": 41.0, "far": 42.0},
            },
        }
        orders = load_orders()
        rates["cell_provenance"] = {
            f"{sec}.{band}.{grp}": {"verified": True, "service": "Ground Advantage",
                                    "rate_basis": "weight", "dest_zip": {"near": "90210",
                                                                        "mid": "80001",
                                                                        "far": "10001"}[grp]}
            for (sec, band, grp), price in orders.required_cells(rates) if price is not None}
        return rates

    def _with(self, rates):
        import json as _json
        import tempfile
        orders = load_orders()
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        _json.dump(rates, fh)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        orders._RATES_PATH = fh.name
        orders._rates_cache["mtime"] = None
        return orders

    @staticmethod
    def _basket(orders, category="Shirts", qty=5):
        """(product, size, qty) triples, the shape shipping_cents() consumes."""
        return [({"category": category}, "M", qty)]

    def test_a_complete_verified_table_switches_the_pricing_rule(self):
        orders = self._with(self._complete_rates())
        self.assertTrue(orders.zone_pricing_ready())
        lines = self._basket(orders)

        flat = orders.shipping_cents(lines, None)
        zoned = orders.shipping_cents(lines, "90210")
        self.assertEqual(orders.shipping_source(None), "estimate")
        self.assertEqual(orders.shipping_source("90210"), "zone")
        self.assertNotEqual(flat, zoned, "a ready table must actually change the charged figure")

    def test_the_same_basket_costs_more_the_further_it_goes(self):
        orders = self._with(self._complete_rates())
        lines = self._basket(orders)
        near = orders.shipping_cents(lines, "90210")
        mid = orders.shipping_cents(lines, "80001")
        far = orders.shipping_cents(lines, "10001")
        self.assertLess(near, mid)
        self.assertLess(mid, far)

    def test_an_unmapped_destination_still_falls_back_to_the_estimate(self):
        """Even with a complete table. A zone is never borrowed from a neighbouring prefix."""
        orders = self._with(self._complete_rates())
        lines = self._basket(orders)
        self.assertEqual(orders.shipping_cents(lines, "59718"),
                         orders.shipping_cents(lines, None))
        self.assertEqual(orders.shipping_source("59718"), "estimate")

    def test_one_unverified_cell_holds_the_whole_switch(self):
        """The gate is all-or-nothing on provenance too, not just on prices being present."""
        rates = self._complete_rates()
        rates["cell_provenance"]["core.3.mid"]["verified"] = False
        orders = self._with(rates)
        self.assertFalse(orders.zone_pricing_ready())
        lines = self._basket(orders)
        self.assertEqual(orders.shipping_cents(lines, "90210"),
                         orders.shipping_cents(lines, None),
                         "one unverified cell must keep every order on the flat ladder")


class TestTheBandPlanAndCeiling(unittest.TestCase):
    """The reduced band plan, and the undercharge it had to close first.

    Heavy bands used to be excluded from required_cells on the grounds that over_max covered them.
    That was backwards: above the top band there was one flat price, so a parcel heavier than the
    weight it was quoted for shipped BELOW cost. An overcharge lands on the buyer and is visible;
    an undercharge comes out of the shop on every such order and nothing says so.
    """

    def test_heavy_bands_are_required(self):
        orders = load_orders()
        want = set(orders.required_bands())
        for band in ("13", "20", "31"):
            with self.subTest(band=band):
                self.assertIn(band, want)

    def test_the_plan_covers_the_deepest_advertised_bulk_tier(self):
        """Shipping has to be quotable for every order the site quotes a PRICE for."""
        import json as _json
        orders = load_orders()
        pricing = _json.load(open(os.path.join(APP_DIR, "pricing.json"), encoding="utf-8"))
        weights = {"Shirts": 7, "Belts": 10, "Shoes": 40, "Bags": 32, "Shorts": 9, "Tracksuits": 28}
        ceiling = orders.max_quotable_oz()
        self.assertIsNotNone(ceiling, "a table with no ceiling guesses above its top band")
        for cat, spec in (pricing.get("categories") or {}).items():
            tiers = [t.get("minQty") for t in (spec.get("tiers") or []) if t.get("minQty")]
            if not tiers or cat not in weights:
                continue
            heaviest = max(tiers) * weights[cat] + 3
            with self.subTest(category=cat, units=max(tiers)):
                self.assertLessEqual(heaviest, ceiling,
                                     f"{max(tiers)} {cat} is {heaviest/16:.1f} lb, above the "
                                     f"{ceiling/16:.0f} lb ceiling — the site advertises a price "
                                     f"for an order it cannot quote shipping on")

    def test_the_one_lb_band_is_not_required(self):
        """No basket this catalogue can assemble lands in (15.99, 16.0] oz."""
        orders = load_orders()
        self.assertNotIn("1", orders.required_bands())

    def test_the_overcharge_report_is_per_group(self):
        """It is a fact about each column's own numbers, not a constant, so it is recomputed."""
        orders = load_orders()
        report = orders.overcharge_report()
        self.assertIn("mid", report)
        self.assertGreater(report["mid"]["worst_overcharge_usd"], 0,
                           "mid is complete enough to price its own skipped bands")
        self.assertEqual(report["far"]["worst_overcharge_usd"], 0.0,
                         "far has no cells yet, so nothing to measure")

    def test_a_table_without_a_ceiling_cannot_switch_on(self):
        orders = load_orders()
        rates = orders.load_rates()
        probe = json.loads(json.dumps(rates))
        probe.pop("max_quotable_oz", None)
        self.assertFalse(orders.zone_pricing_ready(probe))


class TestPackagingAndDimensions(unittest.TestCase):
    """Dimensions do not affect the rate below 1 cu ft — which is why a test box is legitimate —
    and above it they do, in ways a weight-indexed table cannot express."""

    def setUp(self):
        self.orders = load_orders()

    def test_below_one_cubic_foot_dimensions_do_not_matter(self):
        """The fact that makes quoting in a 12x12x11 test box sound."""
        for cu_in in (100, 684, 1584, 1728):
            with self.subTest(cu_in=cu_in):
                self.assertEqual(self.orders.billed_oz(142, cu_in, 6), 142.0)

    def test_above_one_cubic_foot_a_light_parcel_bills_on_volume(self):
        """Counter-intuitive and the reason this matters: at 1 cu ft the dim weight is already
        12.4 lb, so crossing it punishes LIGHT bulky parcels hardest."""
        self.assertGreater(self.orders.billed_oz(142, 2000, 6), 142.0)
        self.assertAlmostEqual(self.orders.billed_oz(142, 2000, 6), 2000 / 139 * 16, places=3)

    def test_a_heavy_parcel_can_cross_one_cubic_foot_and_still_bill_on_weight(self):
        self.assertEqual(self.orders.billed_oz(483, 3400, 6), 483.0)

    def test_dimensional_weight_applies_only_to_zones_five_and_up(self):
        """near is zones 1-4, so a bulky parcel there is billed on actual weight."""
        self.assertEqual(self.orders.billed_oz(142, 2000, 4), 142.0)
        self.assertGreater(self.orders.billed_oz(142, 2000, 5), 142.0)
        self.assertGreater(self.orders.billed_oz(142, 2000, 9), 142.0)

    def test_the_box_surcharges_are_charged_on_the_box_not_the_weight(self):
        self.assertEqual(self.orders.parcel_surcharge_cents(3000, 20), 0)
        self.assertEqual(self.orders.parcel_surcharge_cents(4000, 20), 2100)
        self.assertEqual(self.orders.parcel_surcharge_cents(3000, 24), 450)
        self.assertEqual(self.orders.parcel_surcharge_cents(4000, 24), 2550)

    def test_no_measured_box_blocks_zone_pricing(self):
        """Unmeasured packaging is an undercharge waiting to happen, so it gates go-live."""
        self.assertIsNotNone(self.orders.packaging_problem())
        self.assertFalse(self.orders.zone_pricing_ready())

    def test_a_measured_box_under_a_cubic_foot_clears_the_gate(self):
        rates = json.loads(json.dumps(self.orders.load_rates()))
        rates["packaging"]["boxes"] = [
            {"name": "test", "dims_in": [12, 12, 11], "cu_in": 1584, "verified": True}]
        self.assertIsNone(self.orders.packaging_problem(rates))

    def test_an_oversize_measured_box_is_reported(self):
        rates = json.loads(json.dumps(self.orders.load_rates()))
        rates["packaging"]["boxes"] = [
            {"name": "big", "dims_in": [20, 16, 12], "cu_in": 3840, "verified": True}]
        self.assertIn("$21", self.orders.packaging_problem(rates))

    def test_a_long_measured_box_is_reported(self):
        rates = json.loads(json.dumps(self.orders.load_rates()))
        rates["packaging"]["boxes"] = [
            {"name": "long", "dims_in": [24, 8, 6], "cu_in": 1152, "verified": True}]
        self.assertIn("$4.50", self.orders.packaging_problem(rates))

    def test_an_unverified_box_does_not_count(self):
        rates = json.loads(json.dumps(self.orders.load_rates()))
        rates["packaging"]["boxes"] = [
            {"name": "guessed", "dims_in": [12, 12, 11], "cu_in": 1584, "verified": False}]
        self.assertIsNotNone(self.orders.packaging_problem(rates))

    def test_multi_package_is_declared_unsupported(self):
        """Recorded rather than silently absent: two parcels cost two labels."""
        self.assertFalse(self.orders.load_rates()["multi_package"]["supported"])


class TestRatesAreNotServed(unittest.TestCase):
    """shipping_rates.json must not be reachable over HTTP.

    The browser never reads it: the cart prices off the flat ladder in script.js and the checkout
    figure comes from /api/shipping/quote, computed server-side. So serving it is pure downside. It
    carries the below-Commercial rates Pirate Ship states it may not advertise, and the shipping
    origin ZIP, which for a business run from home is a home address.
    """

    def test_it_is_in_private_files(self):
        src = open(os.path.join(APP_DIR, "server.py"), encoding="utf-8").read()
        block = src[src.index("PRIVATE_FILES = {"):src.index("PRIVATE_DIRS")]
        self.assertIn('"shipping_rates.json"', block)

    def test_the_front_end_does_not_fetch_it(self):
        """If this ever fails, the file has to be served and the guard above must be reconsidered."""
        for name in ("script.js", "index.html"):
            with self.subTest(file=name):
                src = open(os.path.join(APP_DIR, name), encoding="utf-8").read()
                self.assertNotIn("shipping_rates", src)


class TestQuoteValidator(unittest.TestCase):
    """tools/record_quote.py — the check that runs while the screen is still in front of you.

    Every fault this table has had was findable at collection time and was instead found days
    later: a UPS price filed as USPS, a Cubic price filed as weight-based, a 2 lb rate above the
    3 lb one. These assert the validator catches each of those shapes.
    """

    def setUp(self):
        import types as _types
        path = os.path.join(APP_DIR, "tools", "record_quote.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        mod = _types.ModuleType("record_quote_under_test")
        mod.__file__ = path
        exec(compile(src, path, "exec"), mod.__dict__)
        self.rq = mod
        self.orders = load_orders()

    def _rates(self):
        # Includes the 1 lb band, as the real table does — 16.0 oz has its own row.
        bands = ["sub", "1"] + [str(b) for b in range(2, 10)]
        return {
            "origin_zip": "85641", "carrier": "USPS", "service": "Ground Advantage",
            "rate_basis": "weight",
            "zone_groups": {"near": {"zones": [1, 2, 3, 4]}, "mid": {"zones": [5, 6]},
                            "far": {"zones": [7, 8, 9]}},
            "zone_map": {"902": 4, "857": 2, "800": 5, "980": 6, "100": 8},
            "rate_table": {
                "core": dict({b: {"near": None, "mid": None, "far": None} for b in bands},
                             **{"3": {"near": 6.33, "mid": None, "far": None}}),
                "heavy": {}, "over_max": {"near": None, "mid": None, "far": None},
            },
            "cell_provenance": {},
            "advertised_reference": {"rates_by_zone": {
                "sub": {"4": 7.46, "6": 7.86},
                "1": {"4": 8.15, "6": 9.63},
            }},
        }

    def _check(self, rates, **kw):
        args = argparse.Namespace(dest=kw.get("dest", "90210"), oz=kw.get("oz", 32),
                                  box=kw.get("box", "12x12x11"), line=kw.get("line", []),
                                  record=False, next=False)
        return self.rq.check(args, rates, self.orders)

    def test_a_clean_quote_passes(self):
        problems, _n, chosen, cell = self._check(
            self._rates(), oz=64, line=["USPS/Ground Advantage/6.90"])
        self.assertEqual(problems, [])
        self.assertEqual(cell, "core.4.near")
        self.assertEqual(chosen["price_usd"], 6.90)

    def test_a_cubic_only_screen_is_refused_with_the_reason(self):
        problems, _n, _c, _k = self._check(
            self._rates(), box="12x12x3", line=["USPS/Ground Advantage Cubic/7.10"])
        self.assertTrue(problems)
        self.assertTrue(any("BIGGER box" in p for p in problems),
                        "it must say how to make the weight-based line surface")

    def test_another_carrier_cannot_fill_a_cell(self):
        """The first conflict in this file was a UPS price recorded as a USPS rate."""
        problems, _n, _c, _k = self._check(self._rates(), line=["UPS/Ground Saver/5.92"])
        self.assertTrue(any("may fill a cell" in p for p in problems))

    def test_a_group_quoted_below_its_worst_zone_is_refused(self):
        problems, _n, _c, _k = self._check(
            self._rates(), dest="80001", line=["USPS/Ground Advantage/7.20"])
        self.assertTrue(any("dearest zone" in p for p in problems),
                        f"expected a worst-case-zone refusal, got {problems}")

    def test_a_price_that_breaks_the_ladder_is_refused(self):
        problems, _n, _c, _k = self._check(
            self._rates(), oz=80, line=["USPS/Ground Advantage/5.00"])
        self.assertTrue(any("break the ladder" in p for p in problems))

    def test_a_surcharged_package_is_refused(self):
        """A 23-inch side disqualifies Cubic but adds $4.50, so the number is contaminated."""
        problems, _n, _c, _k = self._check(
            self._rates(), box="23x4x4", line=["USPS/Ground Advantage/9.00"])
        self.assertTrue(any("nonstandard length" in p for p in problems))

    def test_an_oversize_package_is_refused(self):
        problems, _n, _c, _k = self._check(
            self._rates(), box="24x24x24", line=["USPS/Ground Advantage/30.00"])
        self.assertTrue(any("oversize volume" in p for p in problems))

    def test_a_price_matching_another_band_is_flagged_as_a_stale_weight(self):
        """The exact shape the 12x12x11 control test made.

        Entered as 2 lb, it returned $7.03 — precisely the verified 5 lb rate — and the weight
        field was not visible in the screenshot. Two bands do not share a price by chance on a
        rising ladder, so the validator names the likely cause rather than leaving it to be
        noticed days later.
        """
        rates = self._rates()
        rates["rate_table"]["core"]["5"] = {"near": 7.03, "mid": None, "far": None}
        problems, _n, _c, _k = self._check(rates, oz=32, line=["USPS/Ground Advantage/7.03"])
        self.assertTrue(any("stale" in p for p in problems),
                        f"expected a stale-weight warning naming band 5, got {problems}")
        self.assertTrue(any("band 5" in p for p in problems))

    def test_the_control_test_figure_is_refused_on_its_own_terms(self):
        """$7.03 at 2 lb is impossible even with the 2 lb cell removed.

        3 lb is $6.33 and verified. A 2 lb parcel must cost less than a 3 lb one on one service.
        This is what makes the reading safe: it never depended on trusting the $6.03 cell.
        """
        rates = self._rates()
        rates["rate_table"]["core"]["2"] = {"near": None, "mid": None, "far": None}
        problems, _n, _c, _k = self._check(rates, oz=32, line=["USPS/Ground Advantage/7.03"])
        self.assertTrue(any("break the ladder" in p for p in problems),
                        f"expected a monotonicity refusal with no 2 lb cell present, got {problems}")

    def test_a_quote_disagreeing_with_a_filled_cell_is_refused(self):
        problems, _n, _c, _k = self._check(
            self._rates(), oz=48, line=["USPS/Ground Advantage/6.99"])
        self.assertTrue(any("already holds" in p for p in problems))

    def test_a_quote_agreeing_with_a_filled_cell_passes(self):
        problems, notes, _c, _k = self._check(
            self._rates(), oz=48, line=["USPS/Ground Advantage/6.33"])
        self.assertEqual(problems, [])
        self.assertTrue(any("agrees with it" in n for n in notes))

    def test_an_unmapped_zip_is_refused(self):
        problems, _n, _c, _k = self._check(
            self._rates(), dest="59718", line=["USPS/Ground Advantage/7.00"])
        self.assertTrue(any("not in zone_map" in p for p in problems))

    # ---- session mode ---------------------------------------------------------------------------
    def test_a_session_parses_dest_box_and_weights(self):
        rows = self.rq.parse_session(
            "# comment\ndest 98101\nbox 12x12x11\n\n"
            "8   USPS/Ground Advantage/6.40, USPS/Ground Advantage Cubic/8.90\n"
            "32  USPS/Ground Advantage/7.10\n")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["dest"], "98101")
        self.assertEqual(rows[0]["box"], "12x12x11")
        self.assertEqual(rows[0]["oz"], 8)
        self.assertEqual(len(rows[0]["line"]), 2, "both service lines must survive the comma split")
        self.assertEqual(rows[0]["line"][1], "USPS/Ground Advantage Cubic/8.90",
                         "a service name containing spaces must not be split")

    def test_a_weight_row_before_dest_and_box_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.rq.parse_session("8  USPS/Ground Advantage/6.40\n")

    def test_a_row_with_no_service_line_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.rq.parse_session("dest 98101\nbox 12x12x11\n8  6.40\n")

    def test_rows_in_one_session_are_checked_against_each_other(self):
        """Not just against the file. Two rows can be individually fine and jointly impossible."""
        rates = self._rates()
        rows = self.rq.parse_session(
            "dest 98001\nbox 12x12x11\n"
            "32  USPS/Ground Advantage/7.10\n"
            "48  USPS/Ground Advantage/6.90\n")
        probe = json.loads(json.dumps(rates))
        verdicts = []
        for row in rows:
            args = argparse.Namespace(dest=row["dest"], oz=row["oz"], box=row["box"],
                                      line=row["line"], record=False, next=False)
            problems, _n, chosen, cell = self.rq.check(args, probe, self.orders)
            verdicts.append(problems)
            if not problems:
                sec, band, grp = cell.split(".")
                probe["rate_table"][sec][band][grp] = chosen["price_usd"]
        self.assertEqual(verdicts[0], [], "the 2 lb row is fine on its own")
        self.assertTrue(any("break the ladder" in p for p in verdicts[1]),
                        "the 3 lb row must fail against the 2 lb row staged before it")

    # ---- a tariff that really does invert --------------------------------------------------------
    def test_an_acknowledged_inversion_stops_being_a_problem(self):
        """Pirate Ship's discount is uneven between bands, so the tariff itself can invert.

        22.8% off at sub-1-lb against 4.0% off at 1 lb, both verified — apply that unevenly to a
        monotonic list price and adjacent bands cross. At zone 6 they do: 1 lb $9.24, 2 lb $8.17.
        An inversion stays an error by default, because it is almost always a bad quote; the
        exception is listed explicitly rather than the rule being switched off.
        """
        orders = load_orders()
        rates = self._rates_with_inversion()
        self.assertTrue(orders.rate_table_problems(rates), "unacknowledged, it must be reported")
        rates["verified_anomalies"] = [["near", "1", "2"]]
        self.assertEqual(orders.rate_table_problems(rates), [],
                         "acknowledged, it must be accepted")

    def test_acknowledging_one_pair_does_not_excuse_another(self):
        orders = load_orders()
        rates = self._rates_with_inversion()
        rates["rate_table"]["core"]["4"] = {"near": 1.00, "mid": None, "far": None}
        rates["verified_anomalies"] = [["near", "1", "2"]]
        self.assertTrue(orders.rate_table_problems(rates),
                        "the 3 lb -> 4 lb inversion is not the one that was acknowledged")

    def test_a_parcel_is_charged_the_cheapest_rate_at_or_above_its_band(self):
        """A shop may declare a heavier weight than it ships, so the 1 lb parcel pays the 2 lb rate.

        That is the real cost and the cheapest honest price for the buyer, and it makes the CHARGED
        ladder monotonic even where the tariff is not.
        """
        orders = load_orders()
        table = self._rates_with_inversion()["rate_table"]
        self.assertEqual(orders.zone_rate_cents(16, "near", table), 817,
                         "a 1 lb parcel must be charged the 2 lb rate, not its own $9.24")
        self.assertEqual(orders.zone_rate_cents(32, "near", table), 817)
        self.assertEqual(orders.zone_rate_cents(8, "near", table), 607,
                         "sub-1-lb is already the cheapest at or above itself")

    def test_the_charged_ladder_never_falls_as_weight_rises(self):
        """The property that matters to a buyer, and it holds by construction now."""
        orders = load_orders()
        table = self._rates_with_inversion()["rate_table"]
        charged = [orders.zone_rate_cents(oz, "near", table) for oz in (8, 16, 32, 48)]
        self.assertEqual(charged, sorted(charged))

    def _rates_with_inversion(self):
        bands = ["sub", "1"] + [str(b) for b in range(2, 10)]
        rates = {
            "origin_zip": "85641", "carrier": "USPS", "service": "Ground Advantage",
            "rate_basis": "weight",
            "zone_groups": {"near": {"zones": [1, 2, 3, 4]}},
            "zone_map": {"902": 4},
            "rate_table": {"core": {b: {"near": None, "mid": None, "far": None} for b in bands},
                           "heavy": {}, "over_max": {"near": None, "mid": None, "far": None}},
        }
        for band, price in (("sub", 6.07), ("1", 9.24), ("2", 8.17), ("3", 9.41)):
            rates["rate_table"]["core"][band]["near"] = price
        return rates

    # ---- the bracket its neighbours impose --------------------------------------------------------
    def test_a_band_must_lie_between_its_filled_neighbours(self):
        """The 1 lb zone 6 case, and the clearest statement of why $9.24 was impossible.

        $6.07 below it and $8.17 above it leave no room for $9.24, whatever $9.24 turns out to be.
        Monotonicity catches the same fault but reports it as "band X is cheaper than band Y",
        naming one of the two and leaving you to work out which is wrong.
        """
        rates = self._rates()
        rates["rate_table"]["core"]["sub"] = {"near": 6.07, "mid": None, "far": None}
        rates["rate_table"]["core"]["2"] = {"near": 8.17, "mid": None, "far": None}
        rates["rate_table"]["core"]["3"] = {"near": None, "mid": None, "far": None}
        problems, _n, _c, _k = self._check(rates, oz=16, line=["USPS/Ground Advantage/9.24"])
        msg = " ".join(problems)
        self.assertIn("band 1 must be", msg)
        self.assertIn("$6.07", msg)
        self.assertIn("$8.17", msg)

    def test_a_value_inside_the_bracket_passes(self):
        rates = self._rates()
        rates["rate_table"]["core"]["sub"] = {"near": 6.07, "mid": None, "far": None}
        rates["rate_table"]["core"]["2"] = {"near": 8.17, "mid": None, "far": None}
        rates["rate_table"]["core"]["3"] = {"near": None, "mid": None, "far": None}
        problems, _n, _c, cell = self._check(rates, oz=16, line=["USPS/Ground Advantage/7.44"])
        self.assertEqual(problems, [])
        self.assertEqual(cell, "core.1.near",
                         "the returned cell key must survive the bracket scan — a loop variable "
                         "named `cell` shadowed it and reported None while every check passed")

    def test_a_band_below_its_floor_is_refused(self):
        rates = self._rates()
        rates["rate_table"]["core"]["sub"] = {"near": 6.07, "mid": None, "far": None}
        problems, _n, _c, _k = self._check(rates, oz=16, line=["USPS/Ground Advantage/5.00"])
        self.assertTrue(any("at least $6.07" in p for p in problems))

    # ---- against the published advertised rate ---------------------------------------------------
    def test_a_quote_at_or_above_the_advertised_rate_is_refused(self):
        """This account prices below Commercial, so it cannot pay more than the advertised figure."""
        problems, _n, _c, _k = self._check(
            self._rates(), oz=8, line=["USPS/Ground Advantage/8.00"])
        self.assertTrue(any("at or above the ADVERTISED" in p for p in problems))

    def test_the_discount_check_compares_only_within_a_band(self):
        """A 1 lb quote must not be judged against sub-1-lb evidence.

        The discount is not uniform across bands: the July 2026 change made sub-1-lb flat and cut
        it far harder than the pound bands. An earlier version pooled every band into one expected
        discount and refused $9.24 at 1 lb zone 6 for being "only 4% off" — measured against
        sub-1-lb cells it had nothing to do with. That was a false positive on a rate that
        reproduced exactly, and the fix is to compare like with like.
        """
        rates = self._rates()
        rates["zone_map"]["354"] = 6
        rates["cell_provenance"]["core.sub.near"] = {
            "verified": True, "service": "Ground Advantage", "rate_basis": "weight",
            "price_usd": 5.83, "dest_zip": "90210"}
        problems, _n, _c, _k = self._check(
            rates, dest="35401", oz=16, line=["USPS/Ground Advantage/9.24"])
        self.assertEqual(problems, [],
                         "a sub-1-lb peer says nothing about the 1 lb band and must not refuse it")

    def test_a_small_discount_is_refused_against_a_peer_in_the_same_band(self):
        """With real evidence in the SAME band, an outlier is still caught."""
        rates = self._rates()
        rates["zone_map"]["354"] = 6
        rates["cell_provenance"]["core.sub.near"] = {
            "verified": True, "service": "Ground Advantage", "rate_basis": "weight",
            "price_usd": 5.83, "dest_zip": "90210"}          # 21.8% below advertised $7.46
        problems, _n, _c, _k = self._check(
            rates, dest="35401", oz=8, line=["USPS/Ground Advantage/7.70"])   # only 2% below
        self.assertTrue(any("same band" in p.lower() or "every verified" in p for p in problems),
                        f"expected a same-band discount refusal, got {problems}")

    def test_cubic_takeover_is_named_as_a_cause_of_twin_prices(self):
        """Once Cubic wins the rate shop it is flat across weight, so bands start repeating."""
        rates = self._rates()
        rates["rate_table"]["core"]["5"] = {"near": 8.17, "mid": None, "far": None}
        problems, _n, _c, _k = self._check(rates, oz=96, line=["USPS/Ground Advantage/8.17"])
        self.assertTrue(any("CUBIC" in p for p in problems),
                        f"the twin-price message must name cubic takeover, got {problems}")

    def test_a_genuine_commercial_rate_passes(self):
        """No false positives on the figures already verified."""
        for dest, oz, price in (("90210", 8, 5.83), ("35401", 8, 6.07)):
            rates = self._rates()
            rates["zone_map"]["354"] = 6
            with self.subTest(dest=dest):
                problems, _n, _c, _k = self._check(rates, dest=dest, oz=oz,
                                                   line=[f"USPS/Ground Advantage/{price}"])
                self.assertEqual(problems, [], f"${price:.2f} is a verified rate and must pass")

    def test_a_band_with_no_advertised_reference_is_not_blocked(self):
        """2-20 lb is redacted in the published sheet, so most bands have no ceiling to check."""
        rates = self._rates()
        problems, _n, _c, _k = self._check(rates, oz=64, line=["USPS/Ground Advantage/6.90"])
        self.assertEqual(problems, [], "a band with no reference must not be refused for that")

    # ---- the destination check -------------------------------------------------------------------
    def test_check_accepts_a_group_worst_case_zip(self):
        rates = self._rates()
        self.assertEqual(self.rq.cmd_check("90210", rates, self.orders), 0,
                         "zone 4 is near's dearest zone in this fixture")

    def test_check_rejects_a_zip_below_the_group_worst_case(self):
        rates = self._rates()
        self.assertEqual(self.rq.cmd_check("85701", rates, self.orders), 1,
                         "zone 2 cannot fill a near cell that must cover zone 4")

    def test_check_rejects_an_unmapped_zip(self):
        self.assertEqual(self.rq.cmd_check("59718", self._rates(), self.orders), 1)

    def test_box_facts_match_the_usps_thresholds(self):
        f = self.rq.box_facts([12, 12, 11], 4)
        self.assertTrue(f["cubic_eligible"])
        self.assertEqual(f["cubic_tier_ft"], 1.0, "0.92 cu ft rounds up to the dearest Cubic tier")
        self.assertEqual(f["fees_triggered"], [], "this box must introduce no surcharge at all")
        self.assertEqual(self.rq.box_facts([12, 12, 11], 6)["fees_triggered"], [],
                         "under 1728 cu in, dim weight must not apply even in zones 5-9")
        self.assertTrue(self.rq.box_facts([13, 13, 13], 6)["fees_triggered"],
                        "over 1728 cu in to zone 6 must flag dimensional weight")
        self.assertEqual(self.rq.box_facts([13, 13, 13], 4)["fees_triggered"], [],
                         "dim weight applies only to zones 5-9")


if __name__ == "__main__":
    unittest.main(verbosity=2)
