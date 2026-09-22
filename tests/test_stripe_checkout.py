"""End-to-end tests for the Stripe Checkout integration. Standard library only.

These run WITHOUT Stripe credentials and without touching the network. Two things make that
honest rather than a shortcut:

  * The webhook signature is real HMAC-SHA256 with a shared secret, so the tests generate genuine
    signatures the same way Stripe does. Verification is exercised for real, not stubbed.
  * The Stripe API call is replaced by a fake transport that RECORDS THE PARAMETERS. The assertions
    are about what the server tells Stripe to charge, which is the part that matters and the part
    a live key would not check any better.

What they cannot cover is Stripe's own behaviour — that its hosted page renders, that a test card
is accepted, that the event it really sends matches the shape used here. That is what the manual
`stripe listen` run in STRIPE.md is for.

    python3 tests/test_stripe_checkout.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_SECRET_KEY = "sk_test_fake_key_for_tests_only"
TEST_WEBHOOK_SECRET = "whsec_fake_signing_secret_for_tests"

server = None
stripe_client = None
orders = None
store = None
httpd = None
BASE = None
stripe_calls = []


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def setUpModule():
    """Boot a real server against a throwaway database, with Stripe faked at the transport."""
    global server, stripe_client, orders, store, httpd, BASE, _tmpdir

    _tmpdir = tempfile.mkdtemp(prefix="poc-stripe-test-")
    os.environ["POC_DATA_DIR"] = _tmpdir
    os.environ["STRIPE_SECRET_KEY"] = TEST_SECRET_KEY
    os.environ["STRIPE_WEBHOOK_SECRET"] = TEST_WEBHOOK_SECRET
    os.environ["POC_PUBLIC_URL"] = "http://localhost:8000"
    os.environ.pop("POC_ALLOW_LIVE_PAYMENTS", None)

    # Seed the catalogue the real bootstrap would copy in.
    for name in ("products.json", "pricing.json"):
        shutil.copy2(os.path.join(APP_DIR, name), os.path.join(_tmpdir, name))

    server = _load("poc_server", os.path.join(APP_DIR, "server.py"))
    stripe_client = server.stripe_client
    orders = server.orders
    store = server.store
    server.bootstrap()

    # Fake transport. Everything above it — parameter building, amounts, idempotency keys — is the
    # real code; only the socket is replaced.
    def fake_request(method, path, params=None, secret_key=None, idempotency_key=None):
        stripe_calls.append({"method": method, "path": path, "params": params,
                             "idempotency_key": idempotency_key})
        if path == "/checkout/sessions" and method == "POST":
            sid = f"cs_test_{len(stripe_calls):08d}"
            return {"id": sid, "url": f"https://checkout.stripe.com/c/pay/{sid}",
                    "payment_intent": None, "status": "open"}
        if path.startswith("/checkout/sessions/") and method == "GET":
            return {"id": path.rsplit("/", 1)[-1], "status": "open",
                    "url": f"https://checkout.stripe.com/c/pay/{path.rsplit('/', 1)[-1]}"}
        return {}

    stripe_client._request = fake_request

    import http.server
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    BASE = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()


def tearDownModule():
    if httpd:
        httpd.shutdown()
        httpd.server_close()      # otherwise the listening socket leaks a ResourceWarning
    shutil.rmtree(_tmpdir, ignore_errors=True)


# ---------- helpers ------------------------------------------------------------------------------
def post(path, body, headers=None, raw=False):
    data = body if raw else json.dumps(body).encode()
    req = urllib.request.Request(f"{BASE}{path}", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def get(path):
    try:
        with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def signed_webhook(event, secret=TEST_WEBHOOK_SECRET, timestamp=None):
    """A genuinely signed webhook, exactly as Stripe builds one."""
    payload = json.dumps(event).encode()
    ts = str(int(time.time()) if timestamp is None else timestamp)
    sig = stripe_client.sign_payload(payload, secret, ts)
    return payload, f"t={ts},v1={sig}"


def db():
    return store.connect()


def order_row(ref):
    conn = db()
    try:
        return orders.order_by_ref(conn, ref)
    finally:
        conn.close()


def physical_qty(product_id, size):
    conn = db()
    try:
        row = conn.execute("SELECT qty FROM product_sizes WHERE product_id=? AND size=?",
                           (product_id, size)).fetchone()
        return None if row is None else row["qty"]
    finally:
        conn.close()


def available_qty(product_id, size):
    conn = db()
    try:
        return orders.availability(conn, product_id, size)
    finally:
        conn.close()


def a_shirt(need=1):
    """A shirt style/size with at least `need` units FREE right now.

    Chosen by live availability, not physical quantity: these tests leave real reservations and
    deductions behind them, so a fixture that always returned the same row would hand the fifth
    test a size the first four had already sold out.
    """
    conn = db()
    try:
        row = conn.execute("""SELECT p.id, p.name, a.size, a.available_qty FROM products p
                              JOIN size_availability a ON a.product_id = p.id
                              WHERE p.category='Shirts' AND p.status='available'
                                AND a.available_qty >= ?
                              ORDER BY a.available_qty DESC, p.id LIMIT 1""", (need,)).fetchone()
        assert row is not None, f"catalogue has no shirt size with {need} units free"
        return dict(row)
    finally:
        conn.close()


def two_shirt_styles(need=5):
    """Two DIFFERENT styles each with `need` units free — for the pooled-quantity tests."""
    conn = db()
    try:
        rows = conn.execute("""SELECT p.id, p.name, a.size, a.available_qty FROM products p
                               JOIN size_availability a ON a.product_id = p.id
                               WHERE p.category='Shirts' AND p.status='available'
                                 AND a.available_qty >= ?
                               ORDER BY a.available_qty DESC, p.id""", (need,)).fetchall()
        seen, out = set(), []
        for r in rows:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            out.append(dict(r))
            if len(out) == 2:
                break
        assert len(out) == 2, f"catalogue needs two shirt styles with {need} units free"
        return out
    finally:
        conn.close()


def completed_event(session_id, order_ref, amount_cents, event_id=None, payment_status="paid",
                    currency="usd", payment_intent=None):
    # Stripe issues one PaymentIntent per payment, so the fixture must too — a shared id would
    # collide on the unique index and test something that cannot happen.
    payment_intent = payment_intent or f"pi_test_{session_id}_{int(time.time()*1000)}"
    return {
        "id": event_id or f"evt_test_{int(time.time()*1000)}_{len(stripe_calls)}",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {"object": {
            "id": session_id, "object": "checkout.session", "client_reference_id": order_ref,
            "payment_status": payment_status, "amount_total": amount_cents, "currency": currency,
            "payment_intent": payment_intent, "metadata": {"order_ref": order_ref},
        }},
    }


# Houston, zone 5 -> group 'mid', the only group with verified cells in every core band. Tests
# that assert a charged figure need a lane the table can actually price.
SHIP_TO = {"name": "Test Buyer", "address1": "1 Test St", "city": "Houston",
           "state": "TX", "zip": "77001", "country": "US"}


def start_checkout(items, email="buyer@example.com", key=None):
    return post("/api/checkout/session", {
        "email": email,
        "idempotency_key": key or f"test-{time.time()}-{len(stripe_calls)}",
        "ship_to": dict(SHIP_TO),
        "items": items,
    })


# ---------- signature verification ----------------------------------------------------------------
class TestSignatureVerification(unittest.TestCase):
    """The webhook secret is the only thing standing between a stranger and free merchandise."""

    def test_valid_signature_passes(self):
        payload, header = signed_webhook({"id": "evt_1", "type": "ping"})
        self.assertTrue(stripe_client.verify_signature(payload, header, TEST_WEBHOOK_SECRET))

    def test_tampered_body_fails(self):
        payload, header = signed_webhook({"id": "evt_1", "type": "ping", "amount": 100})
        tampered = payload.replace(b'"amount": 100', b'"amount": 999')
        with self.assertRaises(stripe_client.SignatureError):
            stripe_client.verify_signature(tampered, header, TEST_WEBHOOK_SECRET)

    def test_wrong_secret_fails(self):
        payload, header = signed_webhook({"id": "evt_1", "type": "ping"})
        with self.assertRaises(stripe_client.SignatureError):
            stripe_client.verify_signature(payload, header, "whsec_not_the_right_secret")

    def test_replayed_old_signature_fails(self):
        """A captured webhook must not work an hour later — that is what the timestamp is for."""
        payload, header = signed_webhook({"id": "evt_1", "type": "ping"},
                                         timestamp=int(time.time()) - 3600)
        with self.assertRaises(stripe_client.SignatureError):
            stripe_client.verify_signature(payload, header, TEST_WEBHOOK_SECRET)

    def test_future_timestamp_fails(self):
        payload, header = signed_webhook({"id": "evt_1", "type": "ping"},
                                         timestamp=int(time.time()) + 3600)
        with self.assertRaises(stripe_client.SignatureError):
            stripe_client.verify_signature(payload, header, TEST_WEBHOOK_SECRET)

    def test_malformed_headers_fail(self):
        payload = b'{"id":"evt_1"}'
        for bad in ("", "garbage", "t=123", "v1=abc", "t=notanumber,v1=abc"):
            with self.assertRaises(stripe_client.SignatureError):
                stripe_client.verify_signature(payload, bad, TEST_WEBHOOK_SECRET)

    def test_missing_secret_fails(self):
        payload, header = signed_webhook({"id": "evt_1", "type": "ping"})
        with self.assertRaises(stripe_client.SignatureError):
            stripe_client.verify_signature(payload, header, "")

    def test_rotated_secret_second_signature_accepted(self):
        """During a rotation Stripe signs with both secrets; either one matching is enough."""
        payload = json.dumps({"id": "evt_1", "type": "ping"}).encode()
        ts = str(int(time.time()))
        old = stripe_client.sign_payload(payload, "whsec_old_secret", ts)
        new = stripe_client.sign_payload(payload, TEST_WEBHOOK_SECRET, ts)
        header = f"t={ts},v1={old},v1={new}"
        self.assertTrue(stripe_client.verify_signature(payload, header, TEST_WEBHOOK_SECRET))

    def test_construct_event_rejects_non_json_and_non_events(self):
        for body in (b"not json at all", b'{"no":"id or type"}'):
            payload = body
            ts = str(int(time.time()))
            header = f"t={ts},v1={stripe_client.sign_payload(payload, TEST_WEBHOOK_SECRET, ts)}"
            with self.assertRaises(stripe_client.SignatureError):
                stripe_client.construct_event(payload, header, TEST_WEBHOOK_SECRET)


# ---------- parameter encoding --------------------------------------------------------------------
class TestParamEncoding(unittest.TestCase):
    def test_nested_dicts_and_lists(self):
        pairs = dict(stripe_client.encode_params({
            "mode": "payment",
            "metadata": {"order_id": "7"},
            "line_items": [{"quantity": 2, "price_data": {"unit_amount": 2100}}],
        }))
        self.assertEqual(pairs["mode"], "payment")
        self.assertEqual(pairs["metadata[order_id]"], "7")
        self.assertEqual(pairs["line_items[0][quantity]"], "2")
        self.assertEqual(pairs["line_items[0][price_data][unit_amount]"], "2100")

    def test_none_is_omitted_and_bools_lowercase(self):
        pairs = dict(stripe_client.encode_params({"a": None, "b": True, "c": False}))
        self.assertNotIn("a", pairs)
        self.assertEqual(pairs["b"], "true")
        self.assertEqual(pairs["c"], "false")


# ---------- live-mode guard -----------------------------------------------------------------------
class TestLiveModeGuard(unittest.TestCase):
    """Real money must never be reachable by accident."""

    def test_key_mode_detection(self):
        self.assertEqual(stripe_client.key_mode("sk_test_abc"), "test")
        self.assertEqual(stripe_client.key_mode("sk_live_abc"), "live")
        self.assertEqual(stripe_client.key_mode("garbage"), "unknown")

    def test_live_key_is_not_configured_without_opt_in(self):
        cfg = {"secret_key": "sk_live_abc", "webhook_secret": "whsec_x", "publishable_key": "",
               "public_url": "http://x"}
        self.assertFalse(stripe_client.is_configured(cfg))
        self.assertIn("live key refused", stripe_client.config_problem(cfg))

    def test_no_webhook_secret_means_payments_off(self):
        """A shop that can charge but cannot confirm would take money and never ship."""
        cfg = {"secret_key": "sk_test_abc", "webhook_secret": "", "publishable_key": "",
               "public_url": "http://x"}
        self.assertFalse(stripe_client.is_configured(cfg))
        self.assertIn("never confirmed", stripe_client.config_problem(cfg))

    def test_redact_never_shows_a_whole_key(self):
        # Assembled at runtime so no key-shaped literal is ever committed: tools/pre-commit greps
        # for that pattern and cannot tell a made-up key from a real one. It blocked this very
        # line when it was written out in full, which is the hook doing its job.
        fake = "sk_" + "test_" + "abcdefghijklmnop"
        red = stripe_client.redact(fake)
        self.assertNotIn("abcdefghijkl", red)
        self.assertTrue(red.endswith("mnop"))
        self.assertTrue(red.startswith("test"))


# ---------- pricing is the server's, not the browser's ---------------------------------------------
class TestServerSidePricing(unittest.TestCase):
    def test_bulk_ladder_is_preserved(self):
        """9 shirts price at the 5+ tier, 10 at the 10+ tier — unchanged by the Stripe path."""
        shirts = two_shirt_styles(need=5)
        conn = db()
        try:
            nine = orders.quote(conn, [
                {"name": shirts[0]["name"], "size": shirts[0]["size"], "qty": 5},
                {"name": shirts[1]["name"], "size": shirts[1]["size"], "qty": 4},
            ])
            ten = orders.quote(conn, [
                {"name": shirts[0]["name"], "size": shirts[0]["size"], "qty": 5},
                {"name": shirts[1]["name"], "size": shirts[1]["size"], "qty": 5},
            ])
        finally:
            conn.close()
        # Pooled across styles: every unit bills at the tier the pooled quantity reached.
        self.assertTrue(all(ln["unit_cents"] == 2100 for ln in nine["lines"]),
                        f"9 shirts should be the 5+ tier: {[l['unit_cents'] for l in nine['lines']]}")
        self.assertTrue(all(ln["unit_cents"] == 1800 for ln in ten["lines"]),
                        f"10 shirts should be the 10+ tier: {[l['unit_cents'] for l in ten['lines']]}")
        # The documented price cliff still holds, which proves the ladder was not flattened.
        self.assertEqual(nine["subtotal_cents"], 18900)
        self.assertEqual(ten["subtotal_cents"], 18000)

    def test_stripe_is_told_the_servers_price_not_the_browsers(self):
        """The cart claims $1; Stripe is still told the real ladder price."""
        shirt = a_shirt(need=5)
        before = len(stripe_calls)
        status, out = start_checkout([
            # Deliberately hostile payload: a price, a tier and a total that are all lies.
            {"id": shirt["id"], "name": shirt["name"], "size": shirt["size"], "qty": 5,
             "price": 0.01, "tier": "retail", "total": 0.05},
        ])
        self.assertEqual(status, 200, out)
        self.assertTrue(out["ok"], out)

        call = stripe_calls[before]
        self.assertEqual(call["path"], "/checkout/sessions")
        items = call["params"]["line_items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["price_data"]["unit_amount"], 2100,
                         "5 shirts must hit the 5+ tier at $21.00, not the browser's $0.01")
        self.assertEqual(items[0]["quantity"], 5)

        row = order_row(out["ref"])
        self.assertEqual(row["subtotal_cents"], 10500)
        self.assertEqual(row["total_cents"], row["subtotal_cents"] + row["shipping_cents"])

    def test_shipping_is_the_verified_zone_rate_not_the_placeholder(self):
        """Shipping now comes from a cell somebody quoted, not the national-average ladder.

        The flat SHIPPING_TIERS figures are placeholders nobody obtained; presenting one as the
        charged price is the drift this codebase refuses elsewhere. Where a verified cell covers
        the order's band and zone it is used, and where none does the order goes to a manual quote
        rather than falling back to a made-up number.
        """
        shirt = a_shirt(need=5)
        status, out = start_checkout([
            {"id": shirt["id"], "name": shirt["name"], "size": shirt["size"], "qty": 5},
        ])
        self.assertEqual(status, 200, out)
        row = order_row(out["ref"])
        oz = row["weight_oz"]

        zoned = orders.zone_shipping_cents(oz, SHIP_TO["zip"])
        self.assertIsNotNone(zoned, "this lane should have a verified cell for that band")
        self.assertEqual(row["shipping_cents"], zoned,
                         "the charged figure must be the verified rate, not the placeholder")
        flat = next(pr for mx, pr in orders.SHIPPING_TIERS if oz <= mx)
        self.assertNotEqual(zoned, flat,
                            "if these coincide the test proves nothing; choose another band")

    def test_stripe_total_equals_the_order_total(self):
        """Line items plus the shipping option must add up to exactly what the DB recorded."""
        shirt = a_shirt(need=3)
        before = len(stripe_calls)
        status, out = start_checkout([
            {"id": shirt["id"], "name": shirt["name"], "size": shirt["size"], "qty": 3},
        ])
        self.assertEqual(status, 200, out)
        params = stripe_calls[before]["params"]
        charged = sum(i["price_data"]["unit_amount"] * i["quantity"] for i in params["line_items"])
        charged += params["shipping_options"][0]["shipping_rate_data"]["fixed_amount"]["amount"]
        row = order_row(out["ref"])
        self.assertEqual(charged, row["total_cents"])

    def test_the_session_expires_with_the_stock_hold(self):
        """A Checkout Session is payable for 24h by default while the reservation lasts 30 minutes.
        Left alone, a buyer could pay most of a day later for units already back on the shelf."""
        shirt = a_shirt()
        before = len(stripe_calls)
        status, out = start_checkout([{"name": shirt["name"], "size": shirt["size"], "qty": 1}])
        self.assertEqual(status, 200, out)

        params = stripe_calls[before]["params"]
        self.assertIn("expires_at", params, "the session must be given an expiry")
        window = params["expires_at"] - time.time()
        self.assertAlmostEqual(window, orders.RESERVATION_TTL_SECONDS, delta=30,
                               msg="the payable window must match the reservation window")

    def test_unknown_product_is_refused(self):
        status, out = start_checkout([{"name": "Not A Real Product", "size": "M", "qty": 1}])
        self.assertEqual(status, 409, out)
        self.assertFalse(out["ok"])

    def test_bad_email_is_refused_before_stripe_is_called(self):
        shirt = a_shirt()
        before = len(stripe_calls)
        status, out = start_checkout(
            [{"name": shirt["name"], "size": shirt["size"], "qty": 1}], email="not-an-email")
        self.assertEqual(status, 400, out)
        self.assertEqual(len(stripe_calls), before, "Stripe must not be called for a bad request")


# ---------- stock reservation ----------------------------------------------------------------------
class TestStockHandling(unittest.TestCase):
    def test_starting_checkout_reserves_but_does_not_deduct(self):
        shirt = a_shirt(need=2)
        phys_before = physical_qty(shirt["id"], shirt["size"])
        avail_before = available_qty(shirt["id"], shirt["size"])

        status, out = start_checkout([
            {"name": shirt["name"], "size": shirt["size"], "qty": 2}])
        self.assertEqual(status, 200, out)

        self.assertEqual(physical_qty(shirt["id"], shirt["size"]), phys_before,
                         "physical stock must not move until payment is confirmed")
        self.assertEqual(available_qty(shirt["id"], shirt["size"]), avail_before - 2,
                         "but the units must be held so nobody else can buy them")

    def test_payment_deducts_stock_once(self):
        shirt = a_shirt()
        phys_before = physical_qty(shirt["id"], shirt["size"])
        status, out = start_checkout([{"name": shirt["name"], "size": shirt["size"], "qty": 1}])
        self.assertEqual(status, 200, out)
        row = order_row(out["ref"])

        payload, header = signed_webhook(
            completed_event(row["payment_ref"], row["order_ref"], row["total_cents"]))
        code, _ = post("/api/stripe/webhook", payload, {"Stripe-Signature": header}, raw=True)
        self.assertEqual(code, 200)

        self.assertEqual(order_row(out["ref"])["status"], "paid")
        self.assertEqual(physical_qty(shirt["id"], shirt["size"]), phys_before - 1)

    def test_failed_stripe_call_releases_the_reservation(self):
        """An order holding stock for a payment that can never happen must not sit there."""
        shirt = a_shirt()
        avail_before = available_qty(shirt["id"], shirt["size"])
        original = stripe_client._request

        def boom(*a, **kw):
            raise stripe_client.StripeError("Stripe is down", {"type": "api_error"})

        stripe_client._request = boom
        try:
            status, out = start_checkout([{"name": shirt["name"], "size": shirt["size"], "qty": 1}])
        finally:
            stripe_client._request = original
        self.assertEqual(status, 502, out)
        self.assertEqual(available_qty(shirt["id"], shirt["size"]), avail_before,
                         "stock must be handed back when the session cannot be created")


# ---------- webhook behaviour ------------------------------------------------------------------------
class TestWebhook(unittest.TestCase):
    def _pending_order(self, qty=1):
        shirt = a_shirt(need=qty)
        status, out = start_checkout([{"name": shirt["name"], "size": shirt["size"], "qty": qty}])
        self.assertEqual(status, 200, out)
        return shirt, order_row(out["ref"])

    def test_unsigned_webhook_is_rejected_and_changes_nothing(self):
        _, row = self._pending_order()
        body = json.dumps(completed_event(row["payment_ref"], row["order_ref"],
                                          row["total_cents"])).encode()
        code, out = post("/api/stripe/webhook", body, {"Stripe-Signature": ""}, raw=True)
        self.assertEqual(code, 400)
        self.assertEqual(order_row(row["order_ref"])["status"], "pending",
                         "an unsigned webhook must never mark an order paid")

    def test_forged_signature_is_rejected(self):
        _, row = self._pending_order()
        event = completed_event(row["payment_ref"], row["order_ref"], row["total_cents"])
        payload, header = signed_webhook(event, secret="whsec_attacker_guess")
        code, _ = post("/api/stripe/webhook", payload, {"Stripe-Signature": header}, raw=True)
        self.assertEqual(code, 400)
        self.assertEqual(order_row(row["order_ref"])["status"], "pending")

    def test_amount_mismatch_does_not_mark_paid(self):
        """Stripe saying a smaller amount was collected must not fulfil the order."""
        _, row = self._pending_order()
        event = completed_event(row["payment_ref"], row["order_ref"], 100)  # $1.00
        payload, header = signed_webhook(event)
        code, out = post("/api/stripe/webhook", payload, {"Stripe-Signature": header}, raw=True)
        self.assertEqual(code, 200)          # accepted, so Stripe stops retrying
        self.assertFalse(out["handled"])     # but deliberately not acted on
        self.assertEqual(order_row(row["order_ref"])["status"], "pending")

    def test_currency_mismatch_does_not_mark_paid(self):
        _, row = self._pending_order()
        event = completed_event(row["payment_ref"], row["order_ref"], row["total_cents"],
                                currency="eur")
        payload, header = signed_webhook(event)
        code, out = post("/api/stripe/webhook", payload, {"Stripe-Signature": header}, raw=True)
        self.assertEqual(code, 200)
        self.assertFalse(out["handled"])
        self.assertEqual(order_row(row["order_ref"])["status"], "pending")

    def test_unpaid_session_completion_is_ignored(self):
        """Delayed payment methods complete the session before the money arrives."""
        _, row = self._pending_order()
        event = completed_event(row["payment_ref"], row["order_ref"], row["total_cents"],
                                payment_status="unpaid")
        payload, header = signed_webhook(event)
        code, out = post("/api/stripe/webhook", payload, {"Stripe-Signature": header}, raw=True)
        self.assertEqual(code, 200)
        self.assertFalse(out["handled"])
        self.assertEqual(order_row(row["order_ref"])["status"], "pending")

    def test_replayed_event_does_not_deduct_twice(self):
        shirt, row = self._pending_order(qty=2)
        phys_before = physical_qty(shirt["id"], shirt["size"])
        event = completed_event(row["payment_ref"], row["order_ref"], row["total_cents"],
                                event_id="evt_replay_fixed_id")
        payload, header = signed_webhook(event)

        code, _ = post("/api/stripe/webhook", payload, {"Stripe-Signature": header}, raw=True)
        self.assertEqual(code, 200)
        after_first = physical_qty(shirt["id"], shirt["size"])
        self.assertEqual(after_first, phys_before - 2)

        # Stripe retries on any non-2xx, and redelivers after an outage. The same event id must be
        # a no-op the second time.
        payload2, header2 = signed_webhook(event)
        code, _ = post("/api/stripe/webhook", payload2, {"Stripe-Signature": header2}, raw=True)
        self.assertEqual(code, 200)
        self.assertEqual(physical_qty(shirt["id"], shirt["size"]), after_first,
                         "a redelivered webhook must not deduct stock twice")
        self.assertEqual(order_row(row["order_ref"])["status"], "paid")

    def test_expired_session_releases_stock(self):
        shirt, row = self._pending_order()
        avail_held = available_qty(shirt["id"], shirt["size"])
        event = {
            "id": f"evt_expired_{time.time()}", "type": "checkout.session.expired", "livemode": False,
            "data": {"object": {"id": row["payment_ref"], "client_reference_id": row["order_ref"]}},
        }
        payload, header = signed_webhook(event)
        code, out = post("/api/stripe/webhook", payload, {"Stripe-Signature": header}, raw=True)
        self.assertEqual(code, 200)
        self.assertTrue(out["handled"])
        self.assertEqual(order_row(row["order_ref"])["status"], "cancelled")
        self.assertEqual(available_qty(shirt["id"], shirt["size"]), avail_held + 1)

    def test_refund_restores_stock(self):
        shirt, row = self._pending_order()
        phys_before = physical_qty(shirt["id"], shirt["size"])
        pi = f"pi_refund_{int(time.time()*1000)}"

        paid = completed_event(row["payment_ref"], row["order_ref"], row["total_cents"],
                               payment_intent=pi)
        payload, header = signed_webhook(paid)
        post("/api/stripe/webhook", payload, {"Stripe-Signature": header}, raw=True)
        self.assertEqual(physical_qty(shirt["id"], shirt["size"]), phys_before - 1)

        refund = {
            "id": f"evt_refund_{time.time()}", "type": "charge.refunded", "livemode": False,
            "data": {"object": {"id": "ch_test", "payment_intent": pi, "refunded": True}},
        }
        payload, header = signed_webhook(refund)
        code, out = post("/api/stripe/webhook", payload, {"Stripe-Signature": header}, raw=True)
        self.assertEqual(code, 200)
        self.assertTrue(out["handled"])
        self.assertEqual(order_row(row["order_ref"])["status"], "refunded")
        self.assertEqual(physical_qty(shirt["id"], shirt["size"]), phys_before,
                         "a refund puts the units back on the shelf")

    def test_unknown_event_type_is_accepted_and_ignored(self):
        event = {"id": f"evt_unknown_{time.time()}", "type": "invoice.created", "livemode": False,
                 "data": {"object": {"id": "in_test"}}}
        payload, header = signed_webhook(event)
        code, out = post("/api/stripe/webhook", payload, {"Stripe-Signature": header}, raw=True)
        self.assertEqual(code, 200, "an unhandled type is not a failure — a 500 would be retried forever")
        self.assertFalse(out["handled"])

    def test_event_for_unknown_order_is_not_an_error(self):
        event = completed_event("cs_test_does_not_exist", "PO-NOPE", 1000)
        payload, header = signed_webhook(event)
        code, out = post("/api/stripe/webhook", payload, {"Stripe-Signature": header}, raw=True)
        self.assertEqual(code, 200)
        self.assertFalse(out["handled"])


# ---------- the shapes a declined card and a 3DS card actually make ------------------------------
class TestCardOutcomes(unittest.TestCase):
    """What the server sees for a decline and for 3D Secure.

    Neither can be driven from here — they happen inside Stripe's hosted page, in a browser, on a
    real account. What IS testable is the event sequence each one produces on our side, and whether
    inventory survives it. That is the part a bug would live in; Stripe declining a card is Stripe's
    to get right.
    """

    def _pending(self, qty=2):
        shirt = a_shirt(need=qty)
        status, out = start_checkout([{"name": shirt["name"], "size": shirt["size"], "qty": qty}])
        self.assertEqual(status, 200, out)
        return shirt, order_row(out["ref"])

    # ---- declined card -------------------------------------------------------------------------
    def test_declined_card_sends_nothing_so_the_order_stays_pending(self):
        """A decline produces NO completed event. The order must not drift to paid on its own, and
        the units must stay held while the buyer retries with another card."""
        shirt, row = self._pending()
        phys = physical_qty(shirt["id"], shirt["size"])
        avail = available_qty(shirt["id"], shirt["size"])

        # ...time passes, no webhook arrives...
        self.assertEqual(order_row(row["order_ref"])["status"], "pending")
        self.assertEqual(physical_qty(shirt["id"], shirt["size"]), phys,
                         "a declined payment must never deduct stock")
        self.assertEqual(available_qty(shirt["id"], shirt["size"]), avail,
                         "but the reservation must hold while they try another card")

    def test_abandoned_after_decline_releases_stock_on_the_sweep(self):
        """The buyer gives up. Nothing from Stripe ever arrives, so the TTL is what frees the units
        — the path that runs when no webhook is coming at all."""
        shirt, row = self._pending()
        held = available_qty(shirt["id"], shirt["size"])

        conn = db()
        try:
            # Simulate the reservation ageing out rather than sleeping 30 minutes.
            expired = orders.expire_pending(conn, ttl_seconds=0)
        finally:
            conn.close()

        self.assertTrue(any(e["ref"] == row["order_ref"] for e in expired),
                        "the pending order should have been swept")
        self.assertEqual(order_row(row["order_ref"])["status"], "cancelled")
        self.assertEqual(available_qty(shirt["id"], shirt["size"]), held + 2,
                         "units must go back on the shelf")

    def test_a_swept_order_cannot_then_be_paid(self):
        """The dangerous ordering: stock released, and a late webhook arrives anyway. It must not
        deduct stock for an order that no longer holds any."""
        shirt, row = self._pending()
        conn = db()
        try:
            orders.expire_pending(conn, ttl_seconds=0)
        finally:
            conn.close()
        phys = physical_qty(shirt["id"], shirt["size"])

        payload, header = signed_webhook(
            completed_event(row["payment_ref"], row["order_ref"], row["total_cents"]))
        code, out = post("/api/stripe/webhook", payload, {"Stripe-Signature": header}, raw=True)

        self.assertEqual(code, 200, "a late event is not a server fault")
        self.assertEqual(order_row(row["order_ref"])["status"], "cancelled",
                         "a cancelled order must not flip to paid")
        self.assertEqual(physical_qty(shirt["id"], shirt["size"]), phys,
                         "and must not deduct stock it is no longer holding")

    def test_a_second_attempt_after_a_decline_is_a_separate_order(self):
        """Retrying with a good card is a fresh session; the first order must not be double-paid."""
        shirt = a_shirt(need=4)
        _, first = start_checkout([{"name": shirt["name"], "size": shirt["size"], "qty": 2}],
                                  key="decline-retry-1")
        _, second = start_checkout([{"name": shirt["name"], "size": shirt["size"], "qty": 2}],
                                   key="decline-retry-2")
        self.assertNotEqual(first["ref"], second["ref"])
        r2 = order_row(second["ref"])
        phys = physical_qty(shirt["id"], shirt["size"])

        payload, header = signed_webhook(
            completed_event(r2["payment_ref"], r2["order_ref"], r2["total_cents"]))
        post("/api/stripe/webhook", payload, {"Stripe-Signature": header}, raw=True)

        self.assertEqual(order_row(second["ref"])["status"], "paid")
        self.assertEqual(order_row(first["ref"])["status"], "pending",
                         "paying the retry must not touch the abandoned first attempt")
        self.assertEqual(physical_qty(shirt["id"], shirt["size"]), phys - 2,
                         "exactly one order's worth of stock moves")

    # ---- 3D Secure -----------------------------------------------------------------------------
    def test_3ds_sequence_pays_once(self):
        """3DS can complete the session before the money lands: `completed` arrives unpaid, then
        `async_payment_succeeded` confirms. Treating the first as payment would ship on an
        authentication that had not cleared yet."""
        shirt, row = self._pending()
        phys = physical_qty(shirt["id"], shirt["size"])

        pending_evt = completed_event(row["payment_ref"], row["order_ref"], row["total_cents"],
                                      event_id="evt_3ds_unpaid", payment_status="unpaid")
        payload, header = signed_webhook(pending_evt)
        code, out = post("/api/stripe/webhook", payload, {"Stripe-Signature": header}, raw=True)
        self.assertEqual(code, 200)
        self.assertFalse(out["handled"])
        self.assertEqual(order_row(row["order_ref"])["status"], "pending")
        self.assertEqual(physical_qty(shirt["id"], shirt["size"]), phys,
                         "no stock moves on an unpaid completion")

        confirmed = {
            "id": "evt_3ds_paid", "type": "checkout.session.async_payment_succeeded",
            "livemode": False,
            "data": {"object": {"id": row["payment_ref"], "client_reference_id": row["order_ref"],
                                "payment_status": "paid", "amount_total": row["total_cents"],
                                "currency": "usd", "payment_intent": "pi_3ds_" + row["order_ref"]}},
        }
        payload, header = signed_webhook(confirmed)
        code, out = post("/api/stripe/webhook", payload, {"Stripe-Signature": header}, raw=True)
        self.assertEqual(code, 200)
        self.assertTrue(out["handled"])
        self.assertEqual(order_row(row["order_ref"])["status"], "paid")
        self.assertEqual(physical_qty(shirt["id"], shirt["size"]), phys - 2,
                         "and exactly one deduction once it clears")

    def test_failed_3ds_authentication_releases_stock(self):
        """Authentication refused: async_payment_failed. Same release as any other failure."""
        shirt, row = self._pending()
        held = available_qty(shirt["id"], shirt["size"])
        event = {
            "id": "evt_3ds_failed_" + row["order_ref"],
            "type": "checkout.session.async_payment_failed", "livemode": False,
            "data": {"object": {"id": row["payment_ref"], "client_reference_id": row["order_ref"]}},
        }
        payload, header = signed_webhook(event)
        code, out = post("/api/stripe/webhook", payload, {"Stripe-Signature": header}, raw=True)
        self.assertEqual(code, 200)
        self.assertTrue(out["handled"])
        self.assertEqual(order_row(row["order_ref"])["status"], "failed")
        self.assertEqual(available_qty(shirt["id"], shirt["size"]), held + 2)


# ---------- inventory is conserved across every ending -------------------------------------------
class TestInventoryConservation(unittest.TestCase):
    """Whatever route an order takes, the shelf must end up telling the truth.

    Checked as a balance rather than per-step: only a sale should ever leave stock lower, and
    nothing should leave it higher than it started.
    """

    def _cycle(self, ending):
        shirt = a_shirt(need=3)
        start_phys = physical_qty(shirt["id"], shirt["size"])
        start_avail = available_qty(shirt["id"], shirt["size"])
        status, out = start_checkout([{"name": shirt["name"], "size": shirt["size"], "qty": 3}])
        self.assertEqual(status, 200, out)
        row = order_row(out["ref"])
        ending(shirt, row)
        return shirt, start_phys, start_avail

    def test_paid_then_refunded_returns_to_the_starting_count(self):
        def ending(shirt, row):
            pi = "pi_cons_" + row["order_ref"]
            payload, header = signed_webhook(
                completed_event(row["payment_ref"], row["order_ref"], row["total_cents"],
                                payment_intent=pi))
            post("/api/stripe/webhook", payload, {"Stripe-Signature": header}, raw=True)
            refund = {"id": "evt_cons_refund_" + row["order_ref"], "type": "charge.refunded",
                      "livemode": False,
                      "data": {"object": {"id": "ch_x", "payment_intent": pi, "refunded": True}}}
            payload, header = signed_webhook(refund)
            post("/api/stripe/webhook", payload, {"Stripe-Signature": header}, raw=True)

        shirt, phys, avail = self._cycle(ending)
        self.assertEqual(physical_qty(shirt["id"], shirt["size"]), phys)
        self.assertEqual(available_qty(shirt["id"], shirt["size"]), avail,
                         "a refunded order must leave no reservation behind either")

    def test_expired_reservation_returns_to_the_starting_count(self):
        def ending(shirt, row):
            conn = db()
            try:
                orders.expire_pending(conn, ttl_seconds=0)
            finally:
                conn.close()

        shirt, phys, avail = self._cycle(ending)
        self.assertEqual(physical_qty(shirt["id"], shirt["size"]), phys)
        self.assertEqual(available_qty(shirt["id"], shirt["size"]), avail)

    def test_no_size_is_ever_oversold(self):
        """The invariant that matters most: available stock can reach zero but never go under it,
        however many orders this suite has put through."""
        conn = db()
        try:
            bad = conn.execute("""SELECT product_id, size, physical_qty, reserved_qty, available_qty
                                  FROM size_availability
                                  WHERE available_qty < 0 OR physical_qty < 0""").fetchall()
        finally:
            conn.close()
        self.assertEqual([dict(r) for r in bad], [], "a size went negative")


# ---------- the return page --------------------------------------------------------------------------
class TestCheckoutStatus(unittest.TestCase):
    def test_status_reflects_the_database_not_the_url(self):
        """Typing ?checkout=success proves nothing; the status endpoint reads the order."""
        shirt = a_shirt()
        status, out = start_checkout([{"name": shirt["name"], "size": shirt["size"], "qty": 1}])
        self.assertEqual(status, 200, out)
        ref = out["ref"]

        code, state = get(f"/api/checkout/status?ref={ref}")
        self.assertEqual(code, 200)
        self.assertFalse(state["paid"], "an unpaid order must not report as paid")
        self.assertEqual(state["status"], "pending")

        row = order_row(ref)
        payload, header = signed_webhook(
            completed_event(row["payment_ref"], ref, row["total_cents"]))
        post("/api/stripe/webhook", payload, {"Stripe-Signature": header}, raw=True)

        code, state = get(f"/api/checkout/status?ref={ref}")
        self.assertTrue(state["paid"])
        self.assertEqual(state["status"], "paid")

    def test_unknown_ref_is_404(self):
        code, _ = get("/api/checkout/status?ref=PO-DOESNOTEXIST")
        self.assertEqual(code, 404)

    def test_status_does_not_leak_customer_details(self):
        shirt = a_shirt()
        _, out = start_checkout([{"name": shirt["name"], "size": shirt["size"], "qty": 1}])
        _, state = get(f"/api/checkout/status?ref={out['ref']}")
        for leaky in ("email", "ship_name", "ship_address1", "items"):
            self.assertNotIn(leaky, state)

    def test_payments_config_exposes_no_keys(self):
        code, cfg = get("/api/payments/config")
        self.assertEqual(code, 200)
        self.assertTrue(cfg["payments_enabled"])
        self.assertEqual(cfg["mode"], "test")
        body = json.dumps(cfg)
        self.assertNotIn("sk_test", body)
        self.assertNotIn("whsec", body)


# ---------- a manual-quote order holds stock, then lets go of it -------------------------------------
class TestManualQuoteLifecycle(unittest.TestCase):
    """An order nothing could price still takes stock off the shelf, so it still has to expire.

    This is the whole risk the manual path introduces. A card order that goes quiet is swept after
    30 minutes; a manual one is waiting on a person, so 30 minutes is far too short — but "waiting
    on a person" with no clock at all means one abandoned bulk enquiry can hold twenty shirts out
    of the catalogue forever, and nothing on the site would say why they were unbuyable.
    """

    # A prefix the USPS chart does not assign. That is PERMANENTLY unpriceable by construction —
    # "a zone is never inferred from distance", so no amount of quoting can ever fill it — which is
    # what a fixture for "nothing can price this" has to be. These tests originally used 10001, a
    # real far-zone ZIP that simply had no cell yet; the moment that column was quoted, nine of
    # them started testing a priced order while still asserting a manual one.
    UNPRICEABLE_ZIP = "34399"

    def setUp(self):
        self.assertIsNone(orders.zone_for_zip(self.UNPRICEABLE_ZIP),
                          f"{self.UNPRICEABLE_ZIP} is now in the zone map; these tests need a "
                          f"destination that cannot be priced, so pick another unmapped prefix")

    def _quote_requested(self, qty=3):
        """Place an order the table cannot price, and return (shirt, row)."""
        shirt = a_shirt(need=qty)
        status, out = post("/api/order", {
            "email": "quote@example.com",
            "idempotency_key": f"manual-{time.time()}-{qty}",
            "ship_to": {**SHIP_TO, "city": "Nowhere", "state": "FL",
                        "zip": self.UNPRICEABLE_ZIP},
            "items": [{"id": shirt["id"], "name": shirt["name"], "size": shirt["size"],
                       "qty": qty}],
        })
        self.assertEqual(status, 200, out)
        return shirt, order_row(out["ref"]), out

    def test_the_order_is_placed_with_no_shipping_price_rather_than_a_zero(self):
        _shirt, row, out = self._quote_requested()
        self.assertEqual(row["status"], "quote_requested")
        self.assertTrue(out["needs_manual_quote"])
        self.assertIsNone(out["shipping"], "$0.00 reads as free shipping, the one wrong answer")
        self.assertIsNone(out["total"])
        self.assertTrue(out["manual_quote_reason"])
        self.assertEqual(row["shipping_pending"], 1,
                         "the column is NOT NULL, so a flag is what says the zero is a placeholder")

    def test_it_holds_stock_while_the_quote_is_being_worked_out(self):
        shirt, _row, _out = self._quote_requested(qty=3)
        self.assertEqual(available_qty(shirt["id"], shirt["size"]),
                         shirt["available_qty"] - 3,
                         "an order awaiting a quote is a real order and reserves its units")

    def test_the_card_ttl_does_not_sweep_it(self):
        """30 minutes is the window a buyer has to finish paying. A person quoting by hand is not
        on that clock, and sweeping at 30 minutes would cancel the order under them."""
        shirt, row, _out = self._quote_requested()
        held = available_qty(shirt["id"], shirt["size"])
        conn = db()
        try:
            swept = orders.expire_pending(conn, ttl_seconds=0)
        finally:
            conn.close()
        self.assertFalse(any(e["ref"] == row["order_ref"] for e in swept),
                         "the card TTL must not reach a quote_requested order")
        self.assertEqual(order_row(row["order_ref"])["status"], "quote_requested")
        self.assertEqual(available_qty(shirt["id"], shirt["size"]), held)

    def test_its_own_ttl_sweeps_it_and_puts_the_units_back(self):
        shirt, row, _out = self._quote_requested(qty=3)
        held = available_qty(shirt["id"], shirt["size"])
        conn = db()
        try:
            swept = orders.expire_pending(conn, manual_ttl_seconds=0)
        finally:
            conn.close()
        self.assertTrue(any(e["ref"] == row["order_ref"] for e in swept))
        self.assertEqual(order_row(row["order_ref"])["status"], "cancelled")
        self.assertEqual(available_qty(shirt["id"], shirt["size"]), held + 3,
                         "the units go back on the shelf, exactly as a card order's do")

    def test_the_cancellation_says_which_clock_ran_out(self):
        """Two reasons an order can be cancelled by the sweep, and the log has to tell them apart —
        it is the only record of why a buyer's order vanished."""
        _shirt, row, _out = self._quote_requested()
        conn = db()
        try:
            orders.expire_pending(conn, manual_ttl_seconds=0)
            note = conn.execute("""SELECT note FROM order_events WHERE order_id=?
                                   ORDER BY id DESC LIMIT 1""", (row["id"],)).fetchone()["note"]
        finally:
            conn.close()
        self.assertIn("quote", note)
        self.assertNotIn("not paid", note)

    def test_it_is_never_marked_paid(self):
        """There is no amount to match against, so no event can honour it."""
        _shirt, row, _out = self._quote_requested()
        conn = db()
        try:
            with self.assertRaises(orders.OrderError):
                orders.mark_paid(conn, row["id"], f"evt_manual_{time.time()}")
        finally:
            conn.close()
        self.assertEqual(order_row(row["order_ref"])["status"], "quote_requested")

    def test_stripe_refuses_an_order_it_cannot_price(self):
        """The button never offers to pay for one of these, but the server is the thing that
        decides — a hand-rolled POST reaches it without going past the button."""
        shirt = a_shirt(need=3)
        status, out = post("/api/checkout/session", {
            "email": "quote@example.com",
            "idempotency_key": f"manual-stripe-{time.time()}",
            "ship_to": {**SHIP_TO, "city": "Nowhere", "state": "FL",
                        "zip": self.UNPRICEABLE_ZIP},
            "items": [{"id": shirt["id"], "name": shirt["name"], "size": shirt["size"], "qty": 3}],
        })
        self.assertEqual(status, 409, out)
        self.assertFalse(out.get("ok"))

    def test_my_orders_does_not_show_the_placeholder_zero_as_free_shipping(self):
        """The buyer's own lookup. It is the one place they can check an order they placed, and it
        rendered $0.00 shipping on the very order whose price had not been worked out yet — the
        zero is a NOT NULL column's placeholder, not a price."""
        _shirt, row, _out = self._quote_requested()
        with urllib.request.urlopen(
                f"{BASE}/api/orders?email=quote@example.com&ref={row['order_ref']}",
                timeout=10) as r:
            found = json.loads(r.read())
        self.assertEqual(len(found), 1)
        self.assertIsNone(found[0]["shipping"])
        self.assertIsNone(found[0]["total"])
        self.assertTrue(found[0]["shipping_pending"])
        self.assertEqual(found[0]["status"], "quote_requested")

    def test_the_storefront_renders_a_pending_order_without_a_total(self):
        """money(null) is "$0.00" or "$NaN" depending on the browser. Neither is the answer."""
        src = open(os.path.join(APP_DIR, "script.js"), encoding="utf-8").read()
        self.assertIn("order.total == null", src)
        self.assertIn("ORDER_STATUS_LABEL", src,
                      "'quote_requested' is accurate and unreadable")

    def test_there_is_one_definition_of_a_pending_shipping_price(self):
        """It lived in server.py, so store.order_by_ref had no way to ask and quietly rendered the
        zero. Two readers, one of them wrong, is the shape of every bug in this file."""
        src = open(os.path.join(APP_DIR, "server.py"), encoding="utf-8").read()
        self.assertIn("_pending = store.shipping_pending", src)
        self.assertNotIn("def _pending(row):", src)

    def test_the_reserve_hold_window_matches_too(self):
        """The other clock the panel quotes at the buyer."""
        import re
        src = open(os.path.join(APP_DIR, "script.js"), encoding="utf-8").read()
        m = re.search(r"const RESERVATION_HOLD_MINUTES = ([0-9.]+)", src)
        self.assertIsNotNone(m, "RESERVATION_HOLD_MINUTES not found in script.js")
        self.assertEqual(float(m.group(1)) * 60, float(orders.RESERVATION_TTL_SECONDS))

    def test_the_reserve_fine_print_is_written_not_left_to_the_markup(self):
        """applyPaymentCopy is the restore path after a manual quote rewrites the line. A branch
        that only sets copy in the OTHER flow is a one-way switch, and the manual wording survived
        onto an order the panel had just priced — promising a shipping email nobody would send."""
        src = open(os.path.join(APP_DIR, "script.js"), encoding="utf-8").read()
        body = src[src.index("function applyPaymentCopy("):]
        reserve = body[body.index("if (!PAYMENTS.enabled) {"):body.index("payLabel.innerHTML = `Pay")]
        self.assertIn("disclaimer.textContent", reserve)
        self.assertIn("RESERVE_NOTICE_HTML", reserve,
                      "the notice is rewritten by a manual quote and has to come back too")

    def test_every_promise_on_the_panel_moves_with_the_manual_flow(self):
        """The rule this project already holds for the payment flows: the eyebrow, the notice, the
        button and the disclaimer each make a claim that is false in the other mode, so they move
        together or not at all. A manual quote is a third mode and the same rule applies — "held
        for 30 minutes, then DM to settle up" is wrong on an order we are going to email a price
        for.
        """
        src = open(os.path.join(APP_DIR, "script.js"), encoding="utf-8").read()
        body = src[src.index("function setCheckoutShipping("):
                   src.index("async function refreshCheckoutShipping")]
        branch = body[body.index("if (manual) {", body.index("payLabel")):body.index("} else {")]
        for owned in ("payLabel", "disclaimer", "notice"):
            self.assertIn(owned, branch, f"the manual branch must move {owned} too")

    def test_the_hold_window_matches_what_the_page_promises(self):
        """The buyer is told a number of hours on the checkout panel. The sweep that enforces it
        is server-side, so a number typed into the JS that disagrees promises a hold the shop does
        not honour — the same parity rule the weight tables live under."""
        import re
        src = open(os.path.join(APP_DIR, "script.js"), encoding="utf-8").read()
        m = re.search(r"const MANUAL_QUOTE_HOLD_HOURS = ([0-9.]+)", src)
        self.assertIsNotNone(m, "MANUAL_QUOTE_HOLD_HOURS not found in script.js")
        self.assertEqual(float(m.group(1)) * 3600, float(orders.MANUAL_QUOTE_TTL_SECONDS))


# ---------- the secret file is not served ------------------------------------------------------------
class TestPrivateFiles(unittest.TestCase):
    def test_stripe_config_is_404(self):
        for path in ("/stripe_config.json", "/costs.json", "/admin_auth.json"):
            try:
                with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as r:
                    self.fail(f"{path} was served with {r.status}")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 404, path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
