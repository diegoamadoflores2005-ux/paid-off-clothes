"""Shipping weights: keyed on categories that really exist, and the same on both sides.

Two separate properties, and the distinction is the whole point of this file.

  PARITY   — script.js and db/orders.py agree. The existing test_pricing_parity covers this.
  VALIDITY — those keys match the categories in products.json.

Only parity was ever checked, and that is exactly why the bug survived: the rename in ff30d32 made
the storefront's categories `Shirts` and `Bags` while both weight tables still said `T-Shirts` and
`Backpacks`. The two files agreed perfectly with each other and were both wrong, so every shirt and
bag fell through to the 8 oz default — a rounding error on a shirt, and a 24 oz undercharge on
every bag sold. A missing key looks identical to "no estimate entered yet", so nothing complained.

    python3 tests/test_shipping_weights.py
"""
import importlib.util
import json
import os
import re
import unittest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_orders():
    spec = importlib.util.spec_from_file_location("_orders", os.path.join(APP_DIR, "db", "orders.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
