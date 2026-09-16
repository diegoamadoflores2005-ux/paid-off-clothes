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

## What is missing right now

Run this on the machine you are testing from — it never prints a secret, so its output is safe to
paste anywhere:

```bash
python3 tools/stripe_preflight.py          # config only, no network
python3 tools/stripe_preflight.py --api    # also asks Stripe whether the key works
```

It checks the key and its mode, the webhook secret, the return URL, and that the database has the
payment columns, then prints the exact next step for anything missing. Exit status is 0 only when
payments would actually be enabled.

As of this commit, on a fresh clone, what it reports missing is **the two credentials and nothing
else** — the code, the schema and the return URL are all in place.

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

## Shipping rates — verify before any real card

The weights are now correct (fixed in `844518a`). **The rates are not**, in two separate ways.

**1. The prices are national-average placeholders.** Ground Advantage is zone-priced. Every number
in `SHIPPING_TIERS` needs replacing with a real quote from Pirate Ship's calculator for the zones
actually shipped to.

**2. The table skips five weight bands.** USPS bills a parcel at its rounded-up pound, but the
table jumps 3 lb → 5 lb → 10 lb. Anything landing in a missing band is charged at the next band up:

| Parcel really weighs | Currently charged | Band |
|---|---|---|
| 4 lb | $12.00 | no 4 lb band — billed at the 5 lb rate |
| 6, 7, 8, 9 lb | $17.00 | no 6–9 lb bands — all billed at the 10 lb rate |

That is not theoretical. Ordinary baskets land there:

| Basket | Weight | Charged | USPS bills |
|---|---|---|---|
| 12 shirts | 5.4 lb | $17.00 | 6 lb |
| 2 pairs of shoes | 5.2 lb | $17.00 | 6 lb |
| 3 bags | 6.2 lb | $17.00 | 7 lb |
| 2 bags + 4 shirts | 5.9 lb | $17.00 | 6 lb |

A buyer with twelve shirts is quoted the ten-pound rate. Nothing is *lost* — the overcharge lands
on the customer, not the shop — but it is the kind of number that loses a bulk sale.

**Worksheet.** Get one quote per row from Pirate Ship for your most common destination zone, then
fill in every band including the five that do not exist yet:

| Weight up to | oz | Current | Real quote |
|---|---|---|---|
| under 1 lb | 15.99 | $5.50 | |
| 1 lb | 16 | $7.61 | |
| 2 lb | 32 | $8.50 | |
| 3 lb | 48 | $9.50 | |
| **4 lb** | **64** | *missing* | |
| 5 lb | 80 | $12.00 | |
| **6 lb** | **96** | *missing* | |
| **7 lb** | **112** | *missing* | |
| **8 lb** | **128** | *missing* | |
| **9 lb** | **144** | *missing* | |
| 10 lb | 160 | $17.00 | |
| over 10 lb | — | $22.00 | |

Edit `SHIPPING_TIERS` in [script.js](script.js) **and the mirrored list in `db/orders.py`** — the
first quotes the buyer, the second charges the card, and `tests/test_shipping_weights.py` fails if
they disagree.

That file also guards the table's shape: every band edge must land on a whole pound, bands must
ascend in both weight and price, and postage must never fall as weight rises. It does **not** check
the prices themselves — only you know what the carrier quoted.

**When you add the missing bands, shrink `KNOWN_MISSING_POUNDS` at the top of that test file to
match, and delete it once it is empty.** The test compares the recorded gap against reality and
fails in both directions, so adding a 6 lb band without updating the set is a loud failure rather
than a silent pass — closing the gap should be written down, not just done.

The category weights are still estimates too. Put one of each on a kitchen scale before real money
moves — postage bills on what the parcel actually weighs, not on this table.

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
