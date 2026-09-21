"""Database access for server.py.

The database is the source of truth. products.json and pricing.json are PROJECTIONS written on
every save, because the storefront fetches them as static files — that is what lets the whole
cutover happen without touching script.js or index.html by a single line.

costs.json, clicks.json and orders.json are projected too. They are not needed by the storefront,
but keeping them current means rollback is always one `git revert` away with no data stranded in
the database.

Every write goes through save_catalog() in a single transaction: either the database and all five
files move together, or nothing does.
"""
import os, sqlite3, threading, importlib.util

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR = os.path.abspath(os.environ.get("POC_DATA_DIR", APP_DIR))
DB_PATH = os.path.join(DIR, "paidoff.db")

_spec = importlib.util.spec_from_file_location("_mig", os.path.join(APP_DIR, "db", "migrate.py"))
_mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mig)

to_cents, from_cents = _mig.to_cents, _mig.from_cents
_lock = threading.Lock()


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")   # a reader is never blocked by a writer
    conn.row_factory = sqlite3.Row
    return conn


def ready():
    return os.path.exists(DB_PATH)


# ---- reads -----------------------------------------------------------------------------------
def products_doc():
    conn = connect()
    try: return _mig.export_products(conn)
    finally: conn.close()


def pricing_doc():
    conn = connect()
    try: return _mig.export_pricing(conn)
    finally: conn.close()


def costs_doc():
    conn = connect()
    try: return _mig.export_costs(conn)
    finally: conn.close()


def clicks_doc():
    conn = connect()
    try: return _mig.export_clicks(conn)
    finally: conn.close()


def shipping_pending(row):
    """True when this order's shipping is awaiting a manual quote.

    The orders.shipping_cents column is NOT NULL, so an order nobody could price stores a zero
    there and this flag is what says that zero is a placeholder. Every path that RENDERS a price
    has to ask, because "$0.00" tells the buyer shipping was free. It lives here, next to the query
    that builds those rows, so a new reader finds it rather than reinventing the zero.

    sqlite3.Row has no .get, and the column is absent on a database that predates migration 005,
    so this tolerates both rather than raising on an old row.
    """
    try:
        return bool(row["shipping_pending"])
    except (IndexError, KeyError):
        return False


def order_by_ref(email, ref):
    """Look up exactly one order, scoped to both the email AND the order reference the buyer was
    shown at checkout. Knowing someone's email alone is not enough to see their order history and
    shipping address — the caller also needs the unguessable ref.

    Returns None on any kind of non-match (blank email, blank ref, wrong ref, wrong email, no such
    order) so the API layer has one shape to send back regardless of which part was wrong. That is
    what keeps the lookup from revealing whether a given email has ever placed an order.
    """
    if not email or not ref:
        return None
    conn = connect()
    try:
        o = conn.execute("SELECT * FROM orders WHERE email=? AND order_ref=?", (email, ref)).fetchone()
        if not o:
            return None
        return {
            "id": o["id"],
            "items": [{"name": r["product_name"], "size": r["size"], "qty": r["qty"],
                       "price": from_cents(r["price_cents"]), "tier": r["tier"]}
                      for r in conn.execute(
                          "SELECT * FROM order_items WHERE order_id=? ORDER BY position",
                          (o["id"],)).fetchall()],
            "subtotal": from_cents(o["subtotal_cents"]),
            # None, not 0.00, while a quote is outstanding. My Orders is the one place a buyer can
            # look up an order they placed, and a zero here told them shipping was free on the
            # very order whose price has not been worked out yet.
            "shipping": None if shipping_pending(o) else from_cents(o["shipping_cents"]),
            "total": None if shipping_pending(o) else from_cents(o["total_cents"]),
            "shipping_pending": shipping_pending(o),
            "weight_oz": o["weight_oz"],
            "ship_to": {"name": o["ship_name"], "address1": o["ship_address1"],
                        "address2": o["ship_address2"], "city": o["ship_city"],
                        "state": o["ship_state"], "zip": o["ship_zip"],
                        "country": o["ship_country"]},
            "time": o["placed_at"], "status": o["status"],
        }
    finally: conn.close()


def all_orders():
    conn = connect()
    try: return _mig.export_orders(conn)
    finally: conn.close()


def bid_for(item):
    conn = connect()
    try:
        r = conn.execute("SELECT * FROM bids WHERE item=?", (item,)).fetchone()
        return {} if not r else {"amount": from_cents(r["amount_cents"]),
                                 "name": r["bidder"], "time": r["placed_at"]}
    finally: conn.close()


# ---- writes ----------------------------------------------------------------------------------
def _project(conn):
    """Rewrite the JSON files from the database. Called inside every write."""
    _mig.write_json(_mig.PRODUCTS, _mig.export_products(conn))
    _mig.write_json(_mig.PRICING, _mig.export_pricing(conn))
    _mig.write_json(_mig.COSTS, _mig.export_costs(conn))
    os.chmod(_mig.COSTS, 0o600)
    _mig.write_json(_mig.CLICKS, _mig.export_clicks(conn), pretty=False)
    _mig.write_json(_mig.ORDERS, _mig.export_orders(conn), pretty=False)


def save_catalog(products, pricing, costs=None):
    """Replace the catalogue wholesale, then project. One transaction."""
    with _lock:
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()
            # Rebuild the catalogue tables. Orders, customers, clicks and bids are untouched:
            # they are transactional history, not catalogue, and a catalogue save must never
            # be able to lose a customer's order.
            for t in ("shipment_lines", "shipments", "product_costs", "product_prices",
                      "product_images", "product_sizes", "products", "pricing_tiers", "categories"):
                cur.execute(f"DELETE FROM {t}")
            _mig.import_docs(cur, products, pricing, costs or {"products": {}, "shipments": []})
            conn.commit()
            _project(conn)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def bump_click(name):
    with _lock:
        conn = connect()
        try:
            conn.execute("""INSERT INTO clicks(product_name,count) VALUES(?,1)
                            ON CONFLICT(product_name) DO UPDATE SET count=count+1""", (name,))
            conn.commit()
            _mig.write_json(_mig.CLICKS, _mig.export_clicks(conn), pretty=False)
        finally: conn.close()


def add_order(order_id, email, order):
    with _lock:
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            t = order.get("time") or 0
            conn.execute("""INSERT INTO customers(email,first_seen,last_seen) VALUES(?,?,?)
                            ON CONFLICT(email) DO UPDATE SET last_seen=excluded.last_seen""",
                         (email, t, t))
            st = order.get("ship_to") or {}
            conn.execute("INSERT OR REPLACE INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (order_id, email, t, to_cents(order.get("subtotal")),
                          to_cents(order.get("shipping")), to_cents(order.get("total")),
                          order.get("weight_oz"), order.get("status", ""), st.get("name", ""),
                          st.get("address1", ""), st.get("address2", ""), st.get("city", ""),
                          st.get("state", ""), st.get("zip", ""), st.get("country", "")))
            for j, it in enumerate(order.get("items") or []):
                conn.execute("INSERT OR REPLACE INTO order_items VALUES (?,?,?,?,?,?,?)",
                             (order_id, j, it["name"], it.get("size", ""), int(it.get("qty", 1)),
                              to_cents(it.get("price")), it.get("tier")))
            conn.commit()
            _mig.write_json(_mig.ORDERS, _mig.export_orders(conn), pretty=False)
        except Exception:
            conn.rollback(); raise
        finally: conn.close()


def project_orders():
    """Rewrite orders.json after an order changes, so rollback stays one step away."""
    with _lock:
        conn = connect()
        try: _mig.write_json(_mig.ORDERS, _mig.export_orders(conn), pretty=False)
        finally: conn.close()


def next_order_id():
    conn = connect()
    try:
        r = conn.execute("SELECT MAX(id) m FROM orders").fetchone()
        return (r["m"] or 3999) + 1
    finally: conn.close()


def set_bid(item, amount, name, when):
    with _lock:
        conn = connect()
        try:
            conn.execute("INSERT OR REPLACE INTO bids VALUES (?,?,?,?)",
                         (item, to_cents(amount), name or "", when))
            conn.commit()
        finally: conn.close()


def subscribe(email, when):
    with _lock:
        conn = connect()
        try:
            cur = conn.execute("SELECT 1 FROM subscribers WHERE email=?", (email,))
            is_new = cur.fetchone() is None
            conn.execute("INSERT OR IGNORE INTO subscribers VALUES (?,?)", (email, when))
            conn.commit()
            return is_new
        finally: conn.close()
