"""Stripe Checkout, over the standard library only.

The project has zero dependencies and that is worth keeping, so this talks to Stripe's REST API
with urllib rather than pulling in the `stripe` SDK. The API is plain form-encoded POSTs and JSON
responses; the only fiddly parts are the bracket notation for nested parameters and the webhook
signature, both of which are implemented here.

CREDENTIALS NEVER LIVE IN THE REPO. Keys come from the environment first, then from
stripe_config.json, which is gitignored, in PRIVATE_FILES and blocked by tools/pre-commit. Nothing
in this file writes a key anywhere, and only the last four characters of a key are ever logged.

TEST MODE IS THE DEFAULT AND LIVE MODE IS REFUSED. A key beginning sk_live_ raises unless
POC_ALLOW_LIVE_PAYMENTS=1 is also set, so switching a real card on is a deliberate act by whoever
runs the server, never a side effect of pasting a key into a config file.
"""
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.stripe.com/v1"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(os.environ.get("POC_DATA_DIR", APP_DIR), "stripe_config.json")

# Stripe signs the timestamp into the payload specifically so a captured webhook cannot be replayed
# later. Five minutes is Stripe's own recommended tolerance.
SIGNATURE_TOLERANCE_SECONDS = 300

TIMEOUT = 20


class StripeError(Exception):
    """An API call failed. `message` is safe to show a buyer; `detail` is for the log."""

    def __init__(self, message, detail=None, status=None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}
        self.status = status


class SignatureError(Exception):
    """A webhook did not come from Stripe, or came too long ago."""


# ---------- configuration -----------------------------------------------------------------------
def _file_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_config():
    """Environment wins over the file, so a deploy can set keys without writing them to disk."""
    f = _file_config()
    return {
        "secret_key": (os.environ.get("STRIPE_SECRET_KEY") or f.get("secret_key") or "").strip(),
        "publishable_key": (os.environ.get("STRIPE_PUBLISHABLE_KEY")
                            or f.get("publishable_key") or "").strip(),
        "webhook_secret": (os.environ.get("STRIPE_WEBHOOK_SECRET")
                           or f.get("webhook_secret") or "").strip(),
        # Where Stripe sends the buyer back. Must be absolute and reachable by their browser.
        "public_url": (os.environ.get("POC_PUBLIC_URL") or f.get("public_url")
                       or "http://localhost:8000").rstrip("/"),
    }


def key_mode(secret_key):
    """'test', 'live', or 'unknown' — read off the key itself, never configured separately."""
    if secret_key.startswith("sk_test_") or secret_key.startswith("rk_test_"):
        return "test"
    if secret_key.startswith("sk_live_") or secret_key.startswith("rk_live_"):
        return "live"
    return "unknown"


def live_allowed():
    return os.environ.get("POC_ALLOW_LIVE_PAYMENTS", "") == "1"


def is_configured(cfg=None):
    """Payments are only 'on' when a key AND a webhook secret are present.

    Without the webhook secret there is no way to confirm a payment, and an order that can be paid
    but never confirmed is worse than one that cannot be paid at all: the buyer is charged and the
    shop never ships. So a half-configured install stays on the reserve flow.
    """
    cfg = cfg or load_config()
    if not cfg["secret_key"] or not cfg["webhook_secret"]:
        return False
    if key_mode(cfg["secret_key"]) == "live" and not live_allowed():
        return False
    return True


def config_problem(cfg=None):
    """Why payments are off, in words — for the operator's console, never for a buyer."""
    cfg = cfg or load_config()
    if not cfg["secret_key"]:
        return "no STRIPE_SECRET_KEY (or secret_key in stripe_config.json)"
    if not cfg["webhook_secret"]:
        return "no STRIPE_WEBHOOK_SECRET — a payment could be taken but never confirmed"
    if key_mode(cfg["secret_key"]) == "live" and not live_allowed():
        return "live key refused: set POC_ALLOW_LIVE_PAYMENTS=1 to take real money"
    if key_mode(cfg["secret_key"]) == "unknown":
        return "secret key is neither sk_test_ nor sk_live_ — check it was pasted whole"
    return None


def redact(secret_key):
    """Keys are never logged whole. Enough tail to tell two keys apart, nothing usable."""
    if not secret_key:
        return "(none)"
    return f"{key_mode(secret_key)}…{secret_key[-4:]}"


# ---------- form encoding -----------------------------------------------------------------------
def encode_params(params, prefix=""):
    """Stripe's bracket notation: {"a": {"b": 1}} -> a[b]=1, lists -> a[0][b]=1.

    Returned as a list of pairs rather than a string so the caller can urlencode once, and so the
    tests can assert on the structure without parsing.
    """
    out = []
    if isinstance(params, dict):
        for key, value in params.items():
            if value is None:
                continue
            out.extend(encode_params(value, f"{prefix}[{key}]" if prefix else str(key)))
    elif isinstance(params, (list, tuple)):
        for i, value in enumerate(params):
            out.extend(encode_params(value, f"{prefix}[{i}]"))
    elif isinstance(params, bool):
        out.append((prefix, "true" if params else "false"))
    else:
        out.append((prefix, str(params)))
    return out


# ---------- API ---------------------------------------------------------------------------------
def _request(method, path, params=None, secret_key=None, idempotency_key=None):
    cfg = load_config()
    secret_key = secret_key or cfg["secret_key"]
    if not secret_key:
        raise StripeError("Payments are not configured.", {"reason": "no secret key"})
    if key_mode(secret_key) == "live" and not live_allowed():
        raise StripeError(
            "Refusing to use a live Stripe key.",
            {"reason": "POC_ALLOW_LIVE_PAYMENTS is not set; this build is test-mode only"},
        )

    url = f"{API_BASE}{path}"
    body = None
    if params:
        encoded = urllib.parse.urlencode(encode_params(params))
        if method == "GET":
            url = f"{url}?{encoded}"
        else:
            body = encoded.encode()

    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {secret_key}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    # Tells Stripe's dashboard what talked to it, and pins the API shape this code was written for.
    req.add_header("Stripe-Version", "2024-06-20")
    req.add_header("User-Agent", "paid-off-clothes/1.0 (stdlib urllib)")
    if idempotency_key:
        # Stripe's own retry guard: the same key returns the first response instead of charging
        # twice, which matters because a timeout does not tell us whether the call landed.
        req.add_header("Idempotency-Key", idempotency_key)

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            err = json.loads(raw).get("error", {})
        except ValueError:
            err = {"message": raw[:500]}
        raise StripeError(
            err.get("message") or "Stripe rejected the request.",
            {"type": err.get("type"), "code": err.get("code"), "param": err.get("param")},
            status=e.code,
        )
    except urllib.error.URLError as e:
        raise StripeError("Could not reach Stripe.", {"reason": str(e.reason)})


def api_post(path, params, idempotency_key=None, secret_key=None):
    return _request("POST", path, params, secret_key=secret_key, idempotency_key=idempotency_key)


def api_get(path, params=None, secret_key=None):
    return _request("GET", path, params, secret_key=secret_key)


def create_checkout_session(order, lines, *, success_url, cancel_url, email=None,
                            idempotency_key=None, secret_key=None):
    """Build a hosted Checkout Session from an order the SERVER has already priced.

    `lines` are the priced rows out of orders.quote() — unit_cents came from the pricing ladder in
    the database, so what Stripe charges is what the shop computed. Nothing here reads a number the
    browser sent.

    Shipping rides as a shipping_option rather than another line item, so the buyer sees it broken
    out on Stripe's page the same way the cart broke it out.
    """
    params = {
        "mode": "payment",
        "success_url": success_url,
        "cancel_url": cancel_url,
        # Both are set deliberately: client_reference_id survives on the session, and the metadata
        # copy rides onto the PaymentIntent too, so a refund event can still be traced to an order.
        "client_reference_id": order["order_ref"],
        "metadata": {"order_id": str(order["order_id"]), "order_ref": order["order_ref"]},
        "payment_intent_data": {
            "metadata": {"order_id": str(order["order_id"]), "order_ref": order["order_ref"]},
        },
        "line_items": [
            {
                "quantity": ln["qty"],
                "price_data": {
                    "currency": "usd",
                    "unit_amount": ln["unit_cents"],
                    "product_data": {
                        "name": ln["name"],
                        "description": f"Size {ln['size']}"
                        + (f" · {ln['tier']} tier" if ln.get("tier") and ln["tier"] != "retail" else ""),
                    },
                },
            }
            for ln in lines
        ],
    }
    if email:
        params["customer_email"] = email
    if order.get("shipping_cents"):
        params["shipping_options"] = [
            {
                "shipping_rate_data": {
                    "type": "fixed_amount",
                    "display_name": "USPS Ground Advantage",
                    "fixed_amount": {"amount": order["shipping_cents"], "currency": "usd"},
                }
            }
        ]
    return api_post("/checkout/sessions", params,
                    idempotency_key=idempotency_key, secret_key=secret_key)


def expire_checkout_session(session_id, secret_key=None):
    """Best-effort: stop a session being paid after we have already released its stock."""
    return api_post(f"/checkout/sessions/{session_id}/expire", {}, secret_key=secret_key)


# ---------- webhook signatures ------------------------------------------------------------------
def parse_signature_header(header):
    """'t=1614556800,v1=abc,v1=def' -> (timestamp, [signatures]). Stripe may send several v1s."""
    timestamp, signatures = None, []
    for part in (header or "").split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            signatures.append(value)
    return timestamp, signatures


def sign_payload(payload, secret, timestamp):
    """The signature Stripe computes. Exposed so tests can produce genuine ones."""
    if isinstance(payload, str):
        payload = payload.encode()
    signed = f"{timestamp}.".encode() + payload
    return hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()


def verify_signature(payload, sig_header, secret, tolerance=SIGNATURE_TOLERANCE_SECONDS, now=None):
    """Raise SignatureError unless this body really came from Stripe, recently.

    Verification runs against the RAW REQUEST BODY. Parsing the JSON and re-serialising it would
    change the bytes and break every signature, so the caller must not hand us a round-tripped
    copy.
    """
    if not secret:
        raise SignatureError("No webhook secret configured.")
    timestamp, signatures = parse_signature_header(sig_header)
    if timestamp is None or not signatures:
        raise SignatureError("Malformed Stripe-Signature header.")
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        raise SignatureError("Malformed timestamp in Stripe-Signature header.")

    now = time.time() if now is None else now
    if tolerance and abs(now - ts) > tolerance:
        raise SignatureError("Webhook timestamp outside the tolerance window.")

    expected = sign_payload(payload, secret, timestamp)
    # compare_digest, and every candidate is checked, so a rotated secret's second signature still
    # verifies without the comparison leaking which one matched.
    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        raise SignatureError("Signature does not match.")
    return True


def construct_event(payload, sig_header, secret, tolerance=SIGNATURE_TOLERANCE_SECONDS, now=None):
    """Verify, then parse. In that order — an unverified body is never JSON-decoded for use."""
    verify_signature(payload, sig_header, secret, tolerance=tolerance, now=now)
    if isinstance(payload, bytes):
        payload = payload.decode()
    try:
        event = json.loads(payload)
    except ValueError:
        raise SignatureError("Webhook body is not JSON.")
    if not isinstance(event, dict) or not event.get("id") or not event.get("type"):
        raise SignatureError("Webhook body is not a Stripe event.")
    return event
