"""Check whether this machine is ready to take a Stripe test payment, and say what is missing.

    python3 tools/stripe_preflight.py            configuration only, no network
    python3 tools/stripe_preflight.py --api      also ask Stripe whether the key works

NEVER PRINTS A SECRET. Keys are shown only as mode + last four characters, and the webhook secret
is reported as present/absent and nothing more. The output is safe to paste into a chat or an issue.

Exit status is 0 when payments would be enabled, 1 when something is missing — so it can gate a
script, not just inform a human.
"""
import importlib.util
import os
import sys

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

OK, WARN, BAD, INFO = "  ok  ", " todo ", " STOP ", "      "


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def line(status, label, detail=""):
    print(f"[{status}] {label}" + (f" — {detail}" if detail else ""))


def main():
    check_api = "--api" in sys.argv
    sc = load("_stripe", os.path.join(APP_DIR, "stripe_client.py"))
    cfg = sc.load_config()
    todo = []

    print("Stripe preflight — Paid Off Clothes")
    print("=" * 66)

    # ---- where configuration is coming from -----------------------------------------------------
    env_keys = [k for k in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "POC_PUBLIC_URL",
                            "POC_ALLOW_LIVE_PAYMENTS") if os.environ.get(k)]
    has_file = os.path.exists(os.path.join(sc.CONFIG_PATH))
    line(INFO, "config source",
         f"environment: {', '.join(env_keys) if env_keys else 'nothing set'}; "
         f"stripe_config.json: {'present' if has_file else 'absent'}")

    # ---- the secret key --------------------------------------------------------------------------
    if not cfg["secret_key"]:
        line(WARN, "secret key", "missing")
        todo.append("Put a TEST secret key in stripe_config.json or STRIPE_SECRET_KEY "
                    "(Dashboard > Developers > API keys, with Test mode ON).")
    else:
        mode = sc.key_mode(cfg["secret_key"])
        if mode == "test":
            line(OK, "secret key", f"test mode ({sc.redact(cfg['secret_key'])})")
        elif mode == "live":
            if sc.live_allowed():
                line(BAD, "secret key", "LIVE key with POC_ALLOW_LIVE_PAYMENTS=1 — this takes real money")
            else:
                line(BAD, "secret key", "live key present but refused; this build is test-mode only")
            todo.append("Swap the live key for a test key (sk_test_...) until go-live is approved.")
        else:
            line(BAD, "secret key", "not recognisable as sk_test_ or sk_live_ — check it pasted whole")
            todo.append("Re-copy the secret key; it should begin sk_test_.")

    # ---- the webhook secret ----------------------------------------------------------------------
    if not cfg["webhook_secret"]:
        line(WARN, "webhook secret", "missing — payments stay OFF without it")
        todo.append("Run `stripe listen --forward-to localhost:8000/api/stripe/webhook` and copy the "
                    "whsec_... it prints into stripe_config.json or STRIPE_WEBHOOK_SECRET.")
    else:
        shape = cfg["webhook_secret"].startswith("whsec_")
        line(OK if shape else BAD, "webhook secret",
             "present" if shape else "present but does not start whsec_ — is that the right value?")
        if not shape:
            todo.append("The webhook secret should begin whsec_. The API key is not interchangeable with it.")

    # ---- return URL -------------------------------------------------------------------------------
    url = cfg["public_url"]
    if not url.startswith(("http://", "https://")):
        line(BAD, "public URL", f"{url} is not absolute — Stripe cannot redirect to it")
        todo.append("Set POC_PUBLIC_URL (or public_url) to an absolute URL.")
    elif "localhost" in url or "127.0.0.1" in url:
        line(OK, "public URL", f"{url} (fine for local testing; must be the real domain in production)")
    else:
        line(OK, "public URL", url)

    # ---- database is migrated ---------------------------------------------------------------------
    try:
        store = load("_store", os.path.join(APP_DIR, "db", "store.py"))
        if not store.ready():
            line(WARN, "database", "no paidoff.db yet — it is built on first `python3 server.py`")
            todo.append("Start the server once so the database and migrations are created.")
        else:
            conn = store.connect()
            try:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(orders)")}
                need = {"payment_provider", "payment_ref", "payment_intent", "payment_mode"}
                missing = need - cols
                if missing:
                    line(BAD, "database", f"orders is missing {', '.join(sorted(missing))}")
                    todo.append("Restart the server — 004_stripe.sql runs on boot and adds them.")
                else:
                    line(OK, "database", "payment columns present, migrations applied")
                pend = conn.execute("SELECT COUNT(*) FROM orders WHERE status='pending'").fetchone()[0]
                if pend:
                    line(INFO, "pending orders", f"{pend} holding stock (released after 30 min)")
            finally:
                conn.close()
    except Exception as e:
        line(WARN, "database", f"could not be inspected: {e}")

    # ---- would the storefront offer card payment? --------------------------------------------------
    print("-" * 66)
    enabled = sc.is_configured(cfg)
    if enabled:
        line(OK, "payments", f"ENABLED in {sc.key_mode(cfg['secret_key'])} mode — "
                             "the checkout button will read “Pay $X”")
    else:
        line(WARN, "payments", f"OFF — {sc.config_problem(cfg)}")
        line(INFO, "", "the storefront falls back to the reserve-and-DM flow, which is not broken")

    # ---- optional: does the key actually work? -----------------------------------------------------
    if check_api:
        print("-" * 66)
        if not cfg["secret_key"]:
            line(INFO, "Stripe API", "skipped, no key to try")
        else:
            try:
                acct = sc.api_get("/account")
                # The account object is the same in both modes; what the key is depends on the key,
                # which we already read off its prefix. Report that rather than guessing from fields.
                line(OK, "Stripe API", f"key accepted by Stripe (account {acct.get('id', '?')}, "
                                       f"key is {sc.key_mode(cfg['secret_key'])} mode)")
            except sc.StripeError as e:
                line(BAD, "Stripe API", f"{e.message} {e.detail}")
                todo.append("The key was rejected by Stripe — check it is current and not revoked.")

    # ---- what to do next ----------------------------------------------------------------------------
    print("=" * 66)
    if todo:
        print("Next steps:")
        for i, t in enumerate(todo, 1):
            print(f"  {i}. {t}")
        print("\nFull walkthrough: STRIPE.md")
    else:
        print("Nothing missing. Run the manual test in STRIPE.md:")
        print("  1. stripe listen --forward-to localhost:8000/api/stripe/webhook")
        print("  2. python3 server.py")
        print("  3. Add 9 shirts to the cart and pay with 4242 4242 4242 4242")
    return 0 if enabled and not todo else 1


if __name__ == "__main__":
    sys.exit(main())
