"""Order lifecycle and inventory. Everything a payment provider will eventually drive.

Two principles run through this file.

PRICES ARE COMPUTED HERE, NEVER ACCEPTED FROM THE BROWSER. The cart posts product names, sizes and
quantities — nothing else. Every price, the bulk tier, shipping and the total are recalculated from
the database. A tampered cart changes what is ordered, never what it costs.

STOCK IS RESERVED, NOT DEDUCTED, UNTIL PAYMENT IS CONFIRMED. A pending order holds units through
inventory_reservations; product_sizes.qty only moves when payment lands. An abandoned checkout
therefore cannot eat inventory, and a refund can hand it back.
"""
import json, os, secrets, sqlite3, threading, time

STATUSES = ("pending", "paid", "fulfilled", "cancelled", "failed", "refunded")

# How long a pending order holds its stock. Without this an abandoned checkout reserves inventory
# forever: the units are invisible to every other buyer but were never sold, so the shop quietly
# runs out of stock it still owns.
RESERVATION_TTL_SECONDS = 30 * 60

# The sweep runs opportunistically on request paths, so this stops a busy shop re-running it on
# every single order.
_SWEEP_MIN_INTERVAL = 60
_last_sweep = 0.0

# Mirrors the shipping block at the top of script.js. Kept in step by test_pricing_parity — which
# did NOT catch the stale "T-Shirts"/"Backpacks" keys, because both files carried the same wrong
# ones. Parity is not correctness; tests/test_shipping_weights.py checks these against the real
# category list as well as against the JS.
CATEGORY_WEIGHT_OZ = {"Shirts": 7, "Belts": 10, "Shoes": 40, "Bags": 32,
                      "Shorts": 9, "Tracksuits": 28}
DEFAULT_WEIGHT_OZ = 8
PACKAGING_OZ = 3
SHIPPING_TIERS = [(15.99, 550), (16, 761), (32, 850), (48, 950), (80, 1200), (160, 1700)]
SHIPPING_OVER_MAX = 2200

_lock = threading.Lock()


class OrderError(Exception):
    """Rejected for a reason the customer should see."""
    def __init__(self, message, detail=None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


# ---- pricing -----------------------------------------------------------------------------------
def _tiers_for(conn, product):
    """Product ladder, else category ladder, else the default — the same three levels as the JS."""
    for scope in (f"prod:{product['id']}", f"cat:{product['category']}", "default"):
        rows = conn.execute(
            "SELECT tier_id, min_qty FROM pricing_tiers WHERE scope=? ORDER BY position", (scope,)
        ).fetchall()
        if rows:
            return [(r["tier_id"], r["min_qty"]) for r in rows]
    return [("retail", 1)]


def _prices_for(conn, product):
    return {r["tier_id"]: r["price_cents"] for r in conn.execute(
        "SELECT tier_id, price_cents FROM product_prices WHERE product_id=?", (product["id"],)
    ).fetchall()}


def price_cents_for(conn, product, qty):
    """The per-unit price at this quantity.

    Charges the CHEAPEST tier the quantity reaches, not the deepest. Since raising quantity only
    adds tiers to the reached set, taking the minimum makes per-unit price non-increasing in
    quantity — a buyer can never pay more per item by buying more, even if someone later types a
    bulk price above the retail one. This matches tierFor() in script.js.
    """
    prices = _prices_for(conn, product)
    best = None
    for tier_id, min_qty in _tiers_for(conn, product):
        if qty < min_qty:
            continue
        p = prices.get(tier_id)
        if p is None:
            continue
        if best is None or p <= best[1]:
            best = (tier_id, p)
    if best is None:
        return ("retail", product["retail_cents"])
    return best


def _weight_oz(product):
    return CATEGORY_WEIGHT_OZ.get(product["category"], DEFAULT_WEIGHT_OZ)


def shipping_cents(lines, dest_zip=None):
    """lines: [(product_row, size, qty)]; dest_zip when the buyer has given an address.

    Zone pricing is used when the verified table can price this order, and the flat ladder
    otherwise — including every cart, which has no address yet. `shipping_source` below says which
    applied, so the checkout panel can show the buyer a real figure rather than an estimate.
    """
    if not lines:
        return 0, 0.0
    oz = sum(_weight_oz(p) * q for p, _s, q in lines) + PACKAGING_OZ
    if dest_zip:
        zoned = zone_shipping_cents(oz, dest_zip)
        if zoned is not None:
            return zoned, float(oz)
    for max_oz, price in SHIPPING_TIERS:
        if oz <= max_oz:
            return price, float(oz)
    return SHIPPING_OVER_MAX, float(oz)


def shipping_source(dest_zip=None):
    """'zone' when this destination would be priced from the verified table, else 'estimate'."""
    if not dest_zip or not zone_pricing_ready():
        return "estimate"
    return "zone" if group_for_zone(zone_for_zip(dest_zip)) else "estimate"


def _resolve_product(conn, product_id, name):
    """Find the product a cart line refers to.

    By id first, because that is stable across renames. Falling back to the display name matters
    because the storefront sends `fullName` — brand plus name, "Amiri 3D Logo Tee" — while the
    products table stores "3D Logo Tee". Matching only on products.name rejected every order.
    """
    if product_id:
        p = conn.execute("SELECT * FROM products WHERE id=?", (str(product_id),)).fetchone()
        if p is not None:
            return p
    if not name:
        return None
    p = conn.execute("SELECT * FROM products WHERE name=?", (name,)).fetchone()
    if p is not None:
        return p
    # "<brand> <name>", the storefront's fullName()
    return conn.execute(
        "SELECT * FROM products WHERE ? = TRIM(CASE WHEN brand='[brand?]' THEN name ELSE brand || ' ' || name END)",
        (name,)).fetchone()


def quote(conn, requested, dest_zip=None):
    """Price a basket from scratch.

    requested: [{name, size, qty}] — exactly what the browser is allowed to influence.
    dest_zip is used for shipping only, and only when the verified zone table can price it.
    Returns the priced lines and the totals, all in integer cents.
    """
    if not requested:
        raise OrderError("Your cart is empty.")

    resolved = []
    for item in requested:
        name = str(item.get("name", "")).strip()
        size = str(item.get("size", "")).strip()
        try:
            qty = int(item.get("qty", 0))
        except (TypeError, ValueError):
            raise OrderError(f"Invalid quantity for “{name}”.")
        if qty < 1:
            raise OrderError(f"Invalid quantity for “{name}”.")
        if qty > 999:
            raise OrderError(f"That quantity is not available for “{name}”.")

        p = _resolve_product(conn, item.get("id"), name)
        if p is None:
            raise OrderError(f"“{name}” is no longer available.")
        if p["status"] == "sold":
            raise OrderError(f"“{name}” is sold out.")
        srow = conn.execute("SELECT * FROM product_sizes WHERE product_id=? AND size=?",
                            (p["id"], size)).fetchone()
        if srow is None:
            raise OrderError(f"Size {size} is not available for “{name}”.")
        resolved.append({"product": p, "size": size, "qty": qty})

    # One line per product+size. A cart that lists the same pair twice is combined rather than
    # rejected, so the quantity check below sees the true total.
    merged = {}
    for r in resolved:
        key = (r["product"]["id"], r["size"])
        if key in merged:
            merged[key]["qty"] += r["qty"]
        else:
            merged[key] = r
    resolved = list(merged.values())

    # Quantity pools across a whole CATEGORY, across styles and sizes — 3 of one tee plus 2 of
    # another is 5 shirts, and all 5 bill at the 5+ price. Same rule as poolUnitsIn() in the JS.
    pooled = {}
    for r in resolved:
        pooled[r["product"]["category"]] = pooled.get(r["product"]["category"], 0) + r["qty"]

    lines, subtotal = [], 0
    for r in resolved:
        tier_id, unit = price_cents_for(conn, r["product"], pooled[r["product"]["category"]])
        line_total = unit * r["qty"]
        subtotal += line_total
        lines.append({
            "product_id": r["product"]["id"], "name": r["product"]["name"], "size": r["size"],
            "qty": r["qty"], "unit_cents": unit, "line_cents": line_total, "tier": tier_id,
            "category": r["product"]["category"],
        })

    ship, oz = shipping_cents([(r["product"], r["size"], r["qty"]) for r in resolved], dest_zip)
    return {"lines": lines, "subtotal_cents": subtotal, "shipping_cents": ship,
            "total_cents": subtotal + ship, "weight_oz": oz,
            "shipping_source": shipping_source(dest_zip)}


# ---- inventory ---------------------------------------------------------------------------------
def availability(conn, product_id, size):
    row = conn.execute("SELECT * FROM size_availability WHERE product_id=? AND size=?",
                       (product_id, size)).fetchone()
    return 0 if row is None else row["available_qty"]


def check_stock(conn, lines):
    """Raise if any line asks for more than is actually free right now."""
    problems = []
    for ln in lines:
        avail = availability(conn, ln["product_id"], ln["size"])
        if ln["qty"] > avail:
            problems.append({"name": ln["name"], "size": ln["size"],
                             "requested": ln["qty"], "available": max(0, avail)})
    if problems:
        first = problems[0]
        msg = (f"Only {first['available']} left of “{first['name']}” in {first['size']}."
               if first["available"] else
               f"“{first['name']}” in {first['size']} just sold out.")
        raise OrderError(msg, {"stock": problems})


# ---- order lifecycle ---------------------------------------------------------------------------
def _log(conn, order_id, frm, to, note=""):
    conn.execute("INSERT INTO order_events(order_id, at, from_status, to_status, note) VALUES (?,?,?,?,?)",
                 (order_id, time.time(), frm, to, note))


def create_order(conn, email, requested, ship_to, idempotency_key=None):
    """Price, validate stock, reserve it, and record a pending order. One transaction."""
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if idempotency_key:
                seen = conn.execute("SELECT order_id FROM payment_events WHERE event_id=?",
                                    (idempotency_key,)).fetchone()
                if seen and seen["order_id"]:
                    existing = conn.execute("SELECT * FROM orders WHERE id=?", (seen["order_id"],)).fetchone()
                    conn.rollback()
                    return {"order_id": existing["id"], "order_ref": existing["order_ref"],
                            "duplicate": True}

            # The ZIP is on the order, so the charged figure is the zone figure — this is the
            # one place shipping is decided for real, and Stripe is built from its result.
            q = quote(conn, requested, dest_zip=(ship_to or {}).get("zip"))
            check_stock(conn, q["lines"])

            now = time.time()
            ref = "PO-" + secrets.token_hex(4).upper()
            conn.execute("""INSERT INTO customers(email, first_seen, last_seen) VALUES(?,?,?)
                            ON CONFLICT(email) DO UPDATE SET last_seen=excluded.last_seen""",
                         (email, now, now))
            cur = conn.execute("""INSERT INTO orders
                (email, placed_at, subtotal_cents, shipping_cents, total_cents, weight_oz, status,
                 ship_name, ship_address1, ship_address2, ship_city, ship_state, ship_zip,
                 ship_country, order_ref, updated_at, currency, inventory_state)
                VALUES (?,?,?,?,?,?, 'pending', ?,?,?,?,?,?,?, ?,?, 'USD', 'reserved')""",
                (email, now, q["subtotal_cents"], q["shipping_cents"], q["total_cents"],
                 q["weight_oz"], ship_to.get("name", ""), ship_to.get("address1", ""),
                 ship_to.get("address2", ""), ship_to.get("city", ""), ship_to.get("state", ""),
                 ship_to.get("zip", ""), ship_to.get("country", ""), ref, now))
            order_id = cur.lastrowid

            for i, ln in enumerate(q["lines"]):
                conn.execute("""INSERT INTO order_items
                    (order_id, position, product_name, size, qty, price_cents, tier, product_id)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (order_id, i, ln["name"], ln["size"], ln["qty"], ln["unit_cents"],
                     ln["tier"], ln["product_id"]))
                conn.execute("""INSERT INTO inventory_reservations
                    (order_id, product_id, size, qty, created_at) VALUES (?,?,?,?,?)""",
                    (order_id, ln["product_id"], ln["size"], ln["qty"], now))

            _log(conn, order_id, None, "pending", "order created, stock reserved")
            if idempotency_key:
                conn.execute("""INSERT INTO payment_events
                    (event_id, provider, event_type, order_id, received_at, payload)
                    VALUES (?,?,?,?,?,?)""",
                    (idempotency_key, "manual", "order.created", order_id, now, None))
            conn.commit()
            return {"order_id": order_id, "order_ref": ref, "duplicate": False,
                    "subtotal_cents": q["subtotal_cents"], "shipping_cents": q["shipping_cents"],
                    "total_cents": q["total_cents"], "lines": q["lines"],
                    "shipping_source": q.get("shipping_source", "estimate")}
        except Exception:
            conn.rollback()
            raise


def _record_event(conn, event_id, event_type, order_id, provider="manual", payload=None):
    """Returns False if this exact event has already been handled.

    The UNIQUE primary key on payment_events is the whole idempotency mechanism: a replayed webhook
    fails the insert and is treated as already-processed, so it cannot deduct stock twice.
    """
    try:
        conn.execute("""INSERT INTO payment_events
            (event_id, provider, event_type, order_id, received_at, payload) VALUES (?,?,?,?,?,?)""",
            (event_id, provider, event_type, order_id, time.time(),
             json.dumps(payload) if payload else None))
        return True
    except sqlite3.IntegrityError:
        return False


def mark_paid(conn, order_id, event_id, provider="manual", payload=None):
    """Confirm payment: convert the reservation into a real deduction."""
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if not _record_event(conn, event_id, "payment.succeeded", order_id, provider, payload):
                conn.rollback()
                return {"ok": True, "duplicate": True}

            o = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if o is None:
                raise OrderError("Unknown order.")
            if o["status"] == "paid":
                conn.commit()
                return {"ok": True, "duplicate": True}
            if o["status"] != "pending":
                raise OrderError(f"Cannot pay an order that is {o['status']}.")

            # Deduct the physical stock the reservation was holding.
            for r in conn.execute("SELECT * FROM inventory_reservations WHERE order_id=?",
                                  (order_id,)).fetchall():
                row = conn.execute("SELECT qty FROM product_sizes WHERE product_id=? AND size=?",
                                   (r["product_id"], r["size"])).fetchone()
                if row is None:
                    raise OrderError("A product in this order no longer exists.")
                if row["qty"] < r["qty"]:
                    # Should be impossible while the reservation stands; refuse rather than
                    # write a negative quantity.
                    raise OrderError("Stock changed since this order was placed.")
                conn.execute("UPDATE product_sizes SET qty = qty - ? WHERE product_id=? AND size=?",
                             (r["qty"], r["product_id"], r["size"]))
            conn.execute("DELETE FROM inventory_reservations WHERE order_id=?", (order_id,))
            conn.execute("""UPDATE orders SET status='paid', inventory_state='deducted',
                            updated_at=? WHERE id=?""", (time.time(), order_id))
            _log(conn, order_id, o["status"], "paid", f"payment confirmed ({provider})")
            conn.commit()
            return {"ok": True, "duplicate": False}
        except Exception:
            conn.rollback()
            raise


def cancel_order(conn, order_id, reason="cancelled", status="cancelled", event_id=None):
    """Release a pending order. Stock was never deducted, so only the reservation is dropped."""
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if event_id and not _record_event(conn, event_id, f"order.{status}", order_id):
                conn.rollback()
                return {"ok": True, "duplicate": True}
            o = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if o is None:
                raise OrderError("Unknown order.")
            if o["status"] in ("cancelled", "failed"):
                conn.commit()
                return {"ok": True, "duplicate": True}
            if o["status"] != "pending":
                raise OrderError(f"Cannot cancel an order that is {o['status']}.")
            conn.execute("DELETE FROM inventory_reservations WHERE order_id=?", (order_id,))
            conn.execute("""UPDATE orders SET status=?, inventory_state='released', updated_at=?
                            WHERE id=?""", (status, time.time(), order_id))
            _log(conn, order_id, o["status"], status, reason)
            conn.commit()
            return {"ok": True, "duplicate": False}
        except Exception:
            conn.rollback()
            raise


def refund_order(conn, order_id, event_id, restore_stock=True, provider="manual"):
    """Money back. Stock goes back on the shelf unless the goods are unsellable."""
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if not _record_event(conn, event_id, "payment.refunded", order_id, provider):
                conn.rollback()
                return {"ok": True, "duplicate": True}
            o = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if o is None:
                raise OrderError("Unknown order.")
            if o["status"] == "refunded":
                conn.commit()
                return {"ok": True, "duplicate": True}
            if o["status"] not in ("paid", "fulfilled"):
                raise OrderError(f"Cannot refund an order that is {o['status']}.")
            if restore_stock and o["inventory_state"] == "deducted":
                for it in conn.execute("SELECT * FROM order_items WHERE order_id=?", (order_id,)).fetchall():
                    if not it["product_id"]:
                        continue
                    exists = conn.execute("SELECT 1 FROM product_sizes WHERE product_id=? AND size=?",
                                          (it["product_id"], it["size"])).fetchone()
                    if exists:
                        conn.execute("UPDATE product_sizes SET qty = qty + ? WHERE product_id=? AND size=?",
                                     (it["qty"], it["product_id"], it["size"]))
            conn.execute("""UPDATE orders SET status='refunded', inventory_state=?, updated_at=?
                            WHERE id=?""",
                         ("restored" if restore_stock else "deducted", time.time(), order_id))
            _log(conn, order_id, o["status"], "refunded",
                 "refunded, stock restored" if restore_stock else "refunded, stock NOT restored")
            conn.commit()
            return {"ok": True, "duplicate": False}
        except Exception:
            conn.rollback()
            raise


def expire_pending(conn, ttl_seconds=RESERVATION_TTL_SECONDS, now=None):
    """Cancel pending orders whose reservation has run out, releasing their stock.

    Safe to run as often as you like. Two independent reasons:

    1. It only ever touches orders that are still 'pending'. A paid, fulfilled, refunded, failed
       or already-cancelled order is invisible to this query, so no completed sale can be undone.
    2. Cancelling releases a RESERVATION — it deletes rows from inventory_reservations and never
       writes to product_sizes.qty. Physical stock was never decremented for a pending order, so
       there is nothing to restore and nothing that can be restored twice.

    The event id is derived from the order and its placement time, so even a concurrent second
    sweep hits the UNIQUE constraint on payment_events and becomes a no-op rather than a repeat.
    """
    now = time.time() if now is None else now
    cutoff = now - ttl_seconds
    rows = conn.execute(
        "SELECT id, order_ref, placed_at FROM orders WHERE status='pending' AND placed_at < ?",
        (cutoff,)).fetchall()
    expired = []
    for r in rows:
        event_id = f"expire_{r['id']}_{int(r['placed_at'])}"
        try:
            res = cancel_order(conn, r["id"], "reservation expired — not paid within "
                               f"{int(ttl_seconds / 60)} minutes", "cancelled", event_id=event_id)
            if not res.get("duplicate"):
                expired.append({"id": r["id"], "ref": r["order_ref"],
                                "age_minutes": round((now - r["placed_at"]) / 60, 1)})
        except OrderError:
            # Raced with something else that moved the order on. Leave it alone.
            continue
    return expired


def sweep_if_due(conn, force=False):
    """Throttled wrapper for the request paths. Returns the orders it expired."""
    global _last_sweep
    now = time.time()
    if not force and (now - _last_sweep) < _SWEEP_MIN_INTERVAL:
        return []
    _last_sweep = now
    return expire_pending(conn)


def set_status(conn, order_id, new_status, note=""):
    """Admin transitions that do not move money or stock (chiefly paid -> fulfilled)."""
    if new_status not in STATUSES:
        raise OrderError("Unknown status.")
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            o = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if o is None:
                raise OrderError("Unknown order.")
            allowed = {"paid": {"fulfilled"}, "fulfilled": {"paid"}, "pending": set(),
                       "cancelled": set(), "failed": set(), "refunded": set()}
            if new_status not in allowed.get(o["status"], set()):
                raise OrderError(
                    f"Cannot move an order from {o['status']} to {new_status} here. "
                    "Payment, cancellation and refunds have their own actions.")
            conn.execute("UPDATE orders SET status=?, updated_at=? WHERE id=?",
                         (new_status, time.time(), order_id))
            _log(conn, order_id, o["status"], new_status, note)
            conn.commit()
            return {"ok": True}
        except Exception:
            conn.rollback()
            raise


# ---- payment provider linkage ------------------------------------------------------------------
# A webhook arrives knowing Stripe's ids and nothing else, so these are the lookups that turn a
# cs_.../pi_... back into one of our orders. Deliberately narrow: they resolve identity, they never
# move money or stock. Every state change still goes through mark_paid / cancel_order /
# refund_order, so there is exactly one place where each transition is implemented.
def attach_payment(conn, order_id, provider, payment_ref, mode="", payment_intent=None):
    """Record which provider object is paying for this order.

    Called right after the Checkout Session is created, so that if the buyer pays and the webhook
    lands before the browser comes back, the event can still find its order.
    """
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("""UPDATE orders SET payment_provider=?, payment_ref=?, payment_mode=?,
                            payment_intent=COALESCE(?, payment_intent), updated_at=?
                            WHERE id=?""",
                         (provider, payment_ref, mode, payment_intent, time.time(), order_id))
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def set_payment_intent(conn, order_id, payment_intent):
    """A session only names its PaymentIntent once payment is under way; a refund names only that.

    Returns True if it was stored. Deliberately NON-FATAL: this id only exists so a later refund
    event can find the order, and the unique index on it can reject a write (the same intent
    already recorded against a different order — an anomaly, but not this payment's problem).
    Letting that raise would abort the webhook handler before mark_paid ran, so Stripe would retry
    a payment that already succeeded and the order would never be marked paid. Money arriving
    matters more than an index being tidy, so the failure is logged and swallowed.
    """
    if not payment_intent:
        return False
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("UPDATE orders SET payment_intent=?, updated_at=? WHERE id=?",
                         (payment_intent, time.time(), order_id))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            conn.rollback()
            print(f"[orders] payment_intent {payment_intent} already belongs to another order; "
                  f"not attaching it to order {order_id}")
            return False
        except Exception:
            conn.rollback()
            raise


def order_by_payment_ref(conn, payment_ref):
    return conn.execute("SELECT * FROM orders WHERE payment_ref=?", (payment_ref,)).fetchone()


def order_by_payment_intent(conn, payment_intent):
    return conn.execute("SELECT * FROM orders WHERE payment_intent=?", (payment_intent,)).fetchone()


def order_by_ref(conn, order_ref):
    return conn.execute("SELECT * FROM orders WHERE order_ref=?", (order_ref,)).fetchone()


def amount_matches(order_row, amount_cents, currency="usd"):
    """Does what the provider says it collected match what we priced?

    The gate that makes server-side pricing mean something. Stripe is told the amount by us, so a
    mismatch means either tampering or a bug — in both cases the order must NOT be marked paid off
    the back of that event.
    """
    if amount_cents is None:
        return False
    if (order_row["currency"] or "USD").lower() != (currency or "usd").lower():
        return False
    return int(amount_cents) == int(order_row["total_cents"])


# ---- zone-aware shipping -------------------------------------------------------------------------
# Ground Advantage is priced on weight AND zone. The flat SHIPPING_TIERS ladder above ignores zone
# and is what runs until the verified table in shipping_rates.json is complete. The switch is
# deliberate and all-or-nothing: quoting some buyers by zone and others off the flat table would be
# worse than either, because two identical baskets to two addresses would be priced by different
# rules with nothing on the page to say so.
_RATES_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "shipping_rates.json")
_rates_cache = {"mtime": None, "data": None}


def load_rates():
    """Read shipping_rates.json, re-reading only when it changes on disk."""
    try:
        mtime = os.path.getmtime(_RATES_PATH)
    except OSError:
        return {}
    if _rates_cache["mtime"] != mtime:
        try:
            with open(_RATES_PATH, encoding="utf-8") as fh:
                _rates_cache["data"] = json.load(fh)
        except (OSError, ValueError):
            _rates_cache["data"] = {}
        _rates_cache["mtime"] = mtime
    return _rates_cache["data"] or {}


def band_for_oz(oz):
    """Which ladder band a parcel falls in: 'sub' under a pound, else the rounded-up pound."""
    if oz <= 15.99:
        return "sub"
    return str(int(-(-float(oz) // 16)))


def zone_for_zip(dest_zip, rates=None):
    """USPS zone for a destination, by its 3-digit prefix. None when unknown.

    Never guessed from distance. The zone chart is published per origin and the mapping is data,
    not arithmetic — an inferred zone silently misprices every order to that prefix.
    """
    rates = load_rates() if rates is None else rates
    digits = "".join(c for c in str(dest_zip or "") if c.isdigit())
    if len(digits) < 3:
        return None
    return (rates.get("zone_map") or {}).get(digits[:3])


def group_for_zone(zone, rates=None):
    rates = load_rates() if rates is None else rates
    for name, spec in (rates.get("zone_groups") or {}).items():
        if zone in (spec.get("zones") or []):
            return name
    return None


def required_cells(rates=None):
    """Every cell that must hold a price before zone pricing may switch on.

    'heavy' is excluded: those bands only occur for bulk shoes and bags, and the fallback covers
    them. The core ladder and the fallback are what every ordinary order needs.
    """
    rates = load_rates() if rates is None else rates
    table = rates.get("rate_table") or {}
    cells = []
    for band, row in (table.get("core") or {}).items():
        for g in ("near", "mid", "far"):
            cells.append((("core", band, g), (row or {}).get(g)))
    for g in ("near", "mid", "far"):
        cells.append((("over_max", "over", g), (table.get("over_max") or {}).get(g)))
    return cells


GROUPS_FAR_TO_NEAR = ("near", "mid", "far")


def rate_table_problems(rates=None):
    """Structural faults that make a table unsafe to charge from. Empty list means sound.

    These are arithmetic facts about postage, not preferences, so a table breaking one of them is
    holding a mistake rather than an unusual price:

    - Postage never falls as weight rises on one service. A heavier parcel costing less means two
      different products got mixed into one column — which is exactly what happened here: a 2 lb
      weight-based quote sat above 3, 4 and 5 lb quotes that were really Ground Advantage CUBIC,
      a volume-priced product that is flat across weight. Nothing in the code noticed.
    - Postage never falls as the destination gets further away. A near price above the matching far
      price means the columns were filled in the wrong order.
    - The over-max fallback is never cheaper than the heaviest band it backs up, or it would be a
      discount for exceeding the table.
    """
    rates = load_rates() if rates is None else rates
    table = rates.get("rate_table") or {}
    problems = []

    bands = table_bands(table)
    for group in GROUPS_FAR_TO_NEAR:
        seen = []  # (pounds, key, price) for bands this group actually prices
        for lb, section, key in bands:
            cell = ((table.get(section) or {}).get(key) or {}).get(group)
            if cell is not None:
                seen.append((lb, key, float(cell)))
        for (lb_a, key_a, price_a), (lb_b, key_b, price_b) in zip(seen, seen[1:]):
            if price_b < price_a:
                problems.append(
                    f"{group}: {key_b} lb at ${price_b:.2f} is cheaper than {key_a} lb at "
                    f"${price_a:.2f} — postage cannot fall as weight rises on one service")
        over = (table.get("over_max") or {}).get(group)
        if over is not None and seen and float(over) < seen[-1][2]:
            problems.append(
                f"{group}: the over-max fallback ${float(over):.2f} is cheaper than the heaviest "
                f"band ({seen[-1][1]} lb at ${seen[-1][2]:.2f})")

    for lb, section, key in bands:
        row = (table.get(section) or {}).get(key) or {}
        priced = [(g, float(row[g])) for g in GROUPS_FAR_TO_NEAR if row.get(g) is not None]
        for (g_a, price_a), (g_b, price_b) in zip(priced, priced[1:]):
            if price_b < price_a:
                problems.append(
                    f"band {key}: {g_b} at ${price_b:.2f} is cheaper than {g_a} at ${price_a:.2f} "
                    f"— postage cannot fall as the destination gets further away")
    return problems


# A table is indexed on weight, so it can only be filled two ways.
#
#   "weight"             every cell is a weight-based quote, one service throughout.
#   "cheapest_available" every cell is whatever the carrier actually charges for THIS shop's
#                        standard package at that weight — which is what Pirate Ship displays,
#                        because it rate shops weight-based against Cubic and shows the winner.
#
# The second is the honest one for a shop that buys the cheapest line on the screen, and it is the
# only one that can hold a Cubic price without lying about what the number means. Its price is a
# function of weight ALONE only while the package is fixed: Cubic is charged on volume, so changing
# the box changes half the table. That is why `quoted_for_package` is mandatory for it.
RATE_BASES = ("weight", "cheapest_available")


def package_problem(rates=None):
    """Why this table's package declaration doesn't support its rate basis, or None."""
    rates = load_rates() if rates is None else rates
    basis = rates.get("rate_basis")
    if basis not in RATE_BASES:
        return f"rate_basis {basis!r} is not one of {RATE_BASES}"
    if basis != "cheapest_available":
        return None
    pkg = rates.get("quoted_for_package") or {}
    dims = pkg.get("dims_in")
    if not (isinstance(dims, list) and len(dims) == 3 and all(
            isinstance(d, (int, float)) and d > 0 for d in dims)):
        return ("rate_basis is 'cheapest_available' but quoted_for_package.dims_in is not three "
                "positive numbers — a rate-shopped price is only a function of weight while the "
                "box is fixed, because the Cubic half of it is charged on volume")
    if not pkg.get("verified"):
        return ("quoted_for_package is not verified — the box has to be measured, not estimated, "
                "before a table built on it can charge anyone")
    return None


def allowed_services(rates=None):
    """Which service names may appear in a cell's provenance under this table's rate basis."""
    rates = load_rates() if rates is None else rates
    if rates.get("rate_basis") == "cheapest_available":
        return tuple(rates.get("rate_shopped_services") or ())
    service = rates.get("service")
    return (service,) if service else ()


def unverified_cells(rates=None):
    """Required cells whose price is not backed by a verified quote this table may hold.

    A price in the table is only as good as its provenance. Every cell needs an entry in
    `cell_provenance` that is marked verified, quoted on a service this table's rate basis allows,
    and — under 'cheapest_available' — quoted for the same package the table declares. A Ground
    Advantage Cubic figure is a real price for a real product; whether it belongs in a given cell
    depends entirely on what that table says its numbers mean.
    """
    rates = load_rates() if rates is None else rates
    prov = rates.get("cell_provenance") or {}
    basis = rates.get("rate_basis")
    allowed = allowed_services(rates)
    pkg_dims = ((rates.get("quoted_for_package") or {}).get("dims_in")
                if basis == "cheapest_available" else None)
    out = []
    for (section, band, group), price in required_cells(rates):
        if price is None:
            continue
        key = f"{section}.{band}.{group}"
        entry = prov.get(key)
        if not entry:
            out.append((key, "no provenance recorded"))
        elif not entry.get("verified"):
            out.append((key, entry.get("why_unverified") or "marked unverified"))
        elif allowed and entry.get("service") not in allowed:
            out.append((key, f"quoted on {entry.get('service')!r}, which this table "
                             f"({basis}) does not accept; allowed: {', '.join(allowed)}"))
        elif basis and entry.get("rate_basis") != basis:
            out.append((key, f"rate basis {entry.get('rate_basis')!r}, not {basis!r}"))
        elif pkg_dims and entry.get("dims_in") != pkg_dims:
            out.append((key, f"quoted for a {entry.get('dims_in')} package, but the table is "
                             f"declared for {pkg_dims} — a rate-shopped cell is only valid for "
                             f"the box it was quoted in"))
    return out


def zone_pricing_ready(rates=None):
    """True only when the zone map and every required cell are present, sound and verified.

    All-or-nothing on purpose. A partly-filled table would fall back per-order, so the same basket
    would cost different amounts depending on which cell happened to be filled.

    Soundness and provenance are part of "ready" rather than a separate warning because the failure
    they catch is silent. A table can be completely filled and still be wrong — mixing a
    volume-priced service into a weight-indexed ladder fills every cell and charges nonsense.
    """
    rates = load_rates() if rates is None else rates
    if not (rates.get("zone_map") or {}):
        return False
    if any(v is None for _key, v in required_cells(rates)):
        return False
    if rate_table_problems(rates) or package_problem(rates):
        return False
    return not unverified_cells(rates)


def table_bands(table):
    """Every band the table actually defines, as (pounds, section, key), lightest first.

    'sub' sorts as 0 pounds. Sorting numerically matters: the JSON keys are strings, so a plain
    key sort puts "11" before "2".
    """
    out = []
    for section in ("core", "heavy"):
        for key in (table.get(section) or {}):
            out.append((0 if key == "sub" else int(key), section, key))
    return sorted(out)


def zone_rate_cents(oz, group, table):
    """What this parcel is charged in one zone group: its own band, or the next one quoted above it.

    The table is a set of quotes at particular pounds, not a complete ladder — 1, 10, 12, 14 and 15
    lb have no row. A parcel landing on an unquoted pound still has to be charged something, and the
    only safe something is the next band UP, because USPS itself bills at the rounded-up pound.

    This used to drop straight to over_max, which is the fallback for parcels heavier than anything
    in the table — the most expensive cell there is. So a 10 lb order paid the heaviest-order price
    while an 11 lb order paid the 11 lb rate, and a parcel of exactly 16.0 oz paid it too. Both
    inversions are the sort a customer notices and nobody else does.
    """
    want_lb = 0 if band_for_oz(oz) == "sub" else int(band_for_oz(oz))
    for lb, section, key in table_bands(table):
        if lb < want_lb:
            continue
        cell = ((table.get(section) or {}).get(key) or {}).get(group)
        if cell is not None:
            return round(float(cell) * 100)
    over = (table.get("over_max") or {}).get(group)
    return None if over is None else round(float(over) * 100)


def zone_shipping_cents(oz, dest_zip, rates=None):
    """Zone-priced postage in cents, or None if this order cannot be priced that way."""
    rates = load_rates() if rates is None else rates
    if not zone_pricing_ready(rates):
        return None
    group = group_for_zone(zone_for_zip(dest_zip, rates), rates)
    if group is None:
        return None
    return zone_rate_cents(oz, group, rates.get("rate_table") or {})
