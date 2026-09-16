# Stripe Checkout

Hosted Checkout, in **test mode**. Card details never touch this site — the buyer is sent to
Stripe's page and comes back. Live payments are refused by default; see [Going live](#going-live).

Nothing here is deployed. The integration is on the branch only.

## How it fits together

```
browser                     server.py                      Stripe
   |  POST /api/checkout/session                             |
   |  {email, ship_to, items:[{name,size,qty}]}               |
   |------------------------->|                              |
   |                          | orders.create_order()        |
   |                          |   prices from the DB ladder  |
   |                          |   checks stock, RESERVES it  |
   |                          |-- POST /v1/checkout/sessions>|
   |<--- {url} ---------------|                              |
   |------------------ redirect to Stripe ------------------>|
   |                          |                              |
   |                          |<-- checkout.session.completed|
   |                          |    verify signature          |
   |                          |    amount == order total?    |
   |                          |    orders.mark_paid()        |
   |                          |      reservation -> deduction|
   |<-- /?checkout=success&ref=PO-XXXX ---------------------- |
   |  GET /api/checkout/status?ref=  (reads the DB, not the URL)
```

**The webhook is what marks an order paid — not the buyer returning to the success page.** A
shopper who closes the tab mid-redirect still gets their order; someone who hand-types
`?checkout=success` gets nothing.

## Setup (test mode)

1. **Keys.** Stripe Dashboard → Developers → API keys, in test mode. Then either:

   ```bash
   cp stripe_config.example.json stripe_config.json   # then edit it
   ```

   or set the environment, which wins over the file and never writes a key to disk:

   ```bash
   export STRIPE_SECRET_KEY=sk_test_...
   export STRIPE_WEBHOOK_SECRET=whsec_...
   export POC_PUBLIC_URL=http://localhost:8000
   ```

   `stripe_config.json` is gitignored, 404s over HTTP, and is blocked by `tools/pre-commit`.

2. **Webhook secret.** Install the [Stripe CLI](https://stripe.com/docs/stripe-cli), then:

   ```bash
   stripe login
   stripe listen --forward-to localhost:8000/api/stripe/webhook
   ```

   It prints `Ready! Your webhook signing secret is whsec_...` — that is the value above. **Leave
   this running**; it is the tunnel Stripe uses to reach a laptop.

3. **Start the server** in another terminal:

   ```bash
   python3 server.py
   ```

   Check it took: `curl -s localhost:8000/api/payments/config` →
   `{"ok": true, "payments_enabled": true, "mode": "test"}`.

   If it says `"payments_enabled": false`, the server prints the reason on the console when a
   checkout is attempted — a missing key, a missing webhook secret, or a refused live key.

## The manual test

Automated tests cover our side (see below). This covers Stripe's.

1. Add a few things to the cart. **Put 9 shirts in** — that exercises the bulk ladder and the price
   cliff, so the test proves the discount survives the handoff.
2. Checkout. The panel should say **Test mode — no real money moves**, and the button should read
   **Pay $X**, not Reserve.
3. Pay with **4242 4242 4242 4242**, any future expiry, any CVC, any ZIP.
4. Watch the `stripe listen` terminal: `checkout.session.completed` → `[200]`.
5. Watch the server terminal: `[stripe] order PO-XXXX paid (251.00 usd)`.
6. You land back on the site: **Test payment complete**, and the cart is empty.
7. Check the books:

   ```bash
   curl -s "localhost:8000/api/checkout/status?ref=PO-XXXX"     # {"paid": true, ...}
   ```

   Admin → Orders shows it paid, and the product's stock has gone down by what was bought.

### Cases worth trying by hand

| Try | Card / action | Expected |
|---|---|---|
| Decline | `4000 0000 0000 0002` | Stays on Stripe, nothing marked paid, stock still reserved |
| Auth required | `4000 0025 0000 3155` | 3DS prompt, then paid as normal |
| Abandon | Close the Stripe tab | Order stays pending; released after 30 min, or on `checkout.session.expired` |
| Cancel | Back-arrow on Stripe's page | Returns to `?checkout=cancelled`, cart intact, nothing charged |
| Refund | Refund the payment in the Dashboard | `charge.refunded` → order `refunded`, stock back on the shelf |
| Replay | `stripe events resend evt_...` | Second delivery is a no-op; stock does **not** move twice |

## Automated tests

```bash
python3 tests/test_stripe_checkout.py
```

39 tests, no credentials and no network. They boot the real server against a throwaway database
and replace only the socket to Stripe, so parameter building, amounts, stock moves and signature
verification are all the real code. What they cover:

- **Signatures** — real HMAC-SHA256: valid passes; tampered body, wrong secret, stale timestamp,
  future timestamp and malformed headers all fail; a rotated secret's second signature is accepted.
- **Server-side pricing** — a cart claiming `price: 0.01` is still charged the ladder price; the
  9-shirt/10-shirt cliff ($189 vs $180) is intact; the shipping figure Stripe is given equals
  `orders.shipping_cents()`; line items plus shipping equal the order total exactly.
- **Stock** — starting checkout reserves without deducting; payment deducts once; a failed Stripe
  call hands the units straight back.
- **Webhooks** — unsigned and forged events change nothing; an amount or currency mismatch is
  accepted but *not* acted on; an unpaid session completion is ignored; a replayed event does not
  deduct twice; expiry releases stock; a refund restores it.
- **Leaks** — `/api/checkout/status` returns no customer details; `/api/payments/config` returns no
  keys; `/stripe_config.json` 404s.

## Going live

**Not yet — this needs the owner's say-so, and a deploy.** When that day comes:

1. Swap in `sk_live_` / `whsec_` values from the live Dashboard.
2. Set `POC_ALLOW_LIVE_PAYMENTS=1`. **Without it a live key is refused**, so real money cannot be
   taken by pasting a key alone.
3. Point `POC_PUBLIC_URL` at the real domain, and register the webhook endpoint at
   `https://<domain>/api/stripe/webhook` in the Dashboard (the CLI tunnel is for laptops only).
4. Re-run the manual test with a real card and a small amount, then refund it.

Before any of that: the shipping rates in `script.js` are national-average placeholders, not real
quotes, and `CATEGORY_WEIGHT_OZ` is estimates. Charging a real card the wrong postage is a real
loss — see the weight-key note in CLAUDE.md.
