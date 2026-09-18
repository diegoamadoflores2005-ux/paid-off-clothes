# Paid Off Clothes

Storefront for a one-person resale business selling high-end designer clothing and accessories
(tees, belts, shoes, backpacks). The goal is a site that reads as a real, curated boutique — not a
generic e-commerce template — where visitors browse rotating stock, bid on a weekly one-off, and
either check out on-site or DM on Instagram to buy.

## Stack

Static site, no build step, no dependencies. At the repo root:

- [index.html](index.html) — full page markup; every dynamic region is an empty container filled by JS
- [products.json](products.json) — **the product/inventory data; edit this to manage stock**
- [pricing.json](pricing.json) — the quantity-discount ladder
- [script.js](script.js) — all rendering and behavior (single global scope, no modules)
- `fonts/` — self-hosted woff2 + `fonts.css`; `images/` — product photos
- [styles.css](styles.css) — all styling, dark theme, CSS custom properties in `:root`
- [server.py](server.py) — stdlib-only dev server + tiny JSON API
- [stripe_client.py](stripe_client.py) — Stripe Checkout over urllib; no SDK, no dependency
- [STRIPE.md](STRIPE.md) — payment setup, the manual test script, and the go-live checklist
- `tests/` — stdlib `unittest`, no credentials needed: `test_stripe_checkout.py` (39) and
  `test_shipping_weights.py` (13 — category weights *and* the rate table). Run both after touching
  pricing, shipping or payments. The Stripe suite covers the shapes a declined card and a 3DS card
  make on the server, and checks inventory is conserved across every ending an order can have.

## Running it

```bash
python3 server.py
```

Serves on http://localhost:8000. Opening `index.html` directly via `file://` works for browsing but
breaks the API-backed features (bids, orders, click stats, newsletter signup submit).

The server sends `Cache-Control: no-store` on everything. Separately, `index.html` links assets with
a manual cache-busting query (`styles.css?v=2`, `script.js?v=2`) for deployed hosts — **bump both
`v=` numbers when you change CSS or JS**, or returning visitors get stale files.

## Inventory

[products.json](products.json) is the **single source of truth**. Nothing about stock lives in
`script.js` any more — `PRODUCTS` starts empty and is filled by `loadProducts()` at startup.

Each record carries: `id`, `name`, `brand`, `category`, `description`, `image`, `images[]`,
`retailPrice`, `bulkPrice`, `bulkMinQty`, `sizes[{size, qty}]`, `status`, `featured`. The file's
own `_README` documents every field; read that before editing.

`productFromRecord()` turns a record into the card shape the rest of the file expects, so all the
old conventions still hold. Key rules:

- **`id` is permanent.** Rename a product freely; never change or reuse an id — a future admin
  dashboard keys on it.
- **`name` must stay unique.** The cart, click tracking and `pricing.json` all key on name.
- **Zero-qty sizes are kept in the file and hidden by the site.** `sizedStock()` used to discard
  them, which lost the size list the moment something sold out; 26 such rows were recovered in the
  conversion. `productFromRecord()` filters them for display, and a style whose sizes are all zero
  drops out of the catalog entirely.
- **`categories` in that file drives the filter tiles**, replacing the old hardcoded `CATEGORIES`.
  A category with no products still renders its tile, which is intentional — it is how a new
  category gets seeded.
- **`featured: true` sets the fallback order for the featured stack**, replacing the hardcoded
  `DEFAULT_FEATURED_ORDER`. Real visitor clicks still outrank it.
- `PRODUCTS` is a `const` array that is **filled, never reassigned** — `renderProducts`,
  `getBidItem`, `computeFeatured`, `loadCart` and the pricing helpers all close over that binding.
- `validateProducts()` warns on duplicate ids or names, unknown categories, non-numeric prices, a
  `bulkPrice` above retail, and empty size lists. **Check the console after editing** — like
  `validatePricing()`, a bad edit degrades quietly.
- **`loadProducts()` must be awaited before anything renders**, and before `loadPricing()`, which
  walks `PRODUCTS` to stash base prices.
- A `file://` page cannot fetch `products.json`, so the catalog renders empty there and logs an
  error. The site needs `server.py`.

Source of the original data is `PO inventory.xlsm` (in ~/Downloads, not in the repo). The
conversion preserved all 20 styles and all 210 live units exactly.

Conventions that other code depends on:

- `p.meta` is the available sizes joined with `" · "` (or `"One Size"`), used as the card's sub-line.
- `p.brand` is the designer name, rendered above the title on every card, in the modal, the featured
  stack, the bid card, and cart/checkout lines. `fullName(p)` joins brand + name; search matches it.
- Unknown brands use the `NEEDS_BRAND` sentinel (`"[brand?]"`), which renders in warning orange via
  `.card-brand-missing` so an unlabelled piece can't pass as a finished listing. `hasBrand(p)` tests
  it. **`NEEDS_BRAND` is a `const` and must stay declared above `PRODUCTS`** — it's referenced during
  that array's initialization, so moving it below reintroduces a temporal-dead-zone crash.
- `p.status` is `"available"` or `"sold"`; `"sold"` disables Buy/Add-to-Cart and hides stock count.
- `p.stock` is optional; absent means one-off (qty 1).
- Names are now unique per card (one card per style), which retired the old collision bug where the
  cart, click tracking, and the featured stack all keyed on a `name` repeated across sizes.
- The item modal's size chips double as the availability breakdown and the picker: each shows its
  units ("M ×5"), clicking one selects it, and Buy Now / Add to Cart stay disabled reading
  "Select a Size" until one is. A single-size style auto-selects. A quantity stepper appears once a
  size is chosen and is capped at that size's units.
- **Everything quantity-related goes through `clampQty(p, size, n)`**, which floors at 1 and ceilings
  at `unitsFor(p, size)`. It runs on the modal stepper, on cart steppers, on top-ups, and again in
  `loadCart`, so a hand-edited `localStorage` can't push a line past available stock.
- Sum money with `lineTotal(lines)` (price x qty), never by reducing over `price` alone.
- Adding a category means adding it to `CATEGORIES` too.

`images/logo.png` is the brand logo, 700x452 with a **transparent** background, used by the opening
reveal and safe to drop on any background. It was keyed out of the supplied black-backed original
(kept as `images/logo-original.png`) by treating luminance above a 0.16 threshold as fully opaque
and ramping only the narrow antialiasing band just above black — so the grey skyline and off-white
script stay solid rather than becoming semi-transparent, which is what a naive luminance-as-alpha
pass produces.

`images/skyline.png` (672x152, transparent) is the skyline band lifted straight out of the logo, so
the backdrop is the brand's own artwork rather than a stock photo — no licensing question and it
matches the mark exactly. Regenerate it by cropping `images/logo.png` above the first row
containing a run of near-white pixels (the top of "PAIDOFF").

Three things to keep in mind when touching the reveal:
- **Keep `.intro-sheen` a direct child of `.intro`, not of `.intro-stage`.** Nested inside the stage
  it inherits the spin and rotates with the logo instead of sweeping the screen.
- **Do not add `backface-visibility: hidden`** to the spinning element. It turns through two full
  revolutions, so hiding the back face blanks it for half of every turn — it strobes instead of
  spinning.
- Animate transform and opacity only. An earlier version animated `filter: blur()`, which drops the
  sequence off the compositor and makes the spin hitch.

Images live in `images/`. All 21 style photos were extracted from `PO inventory.xlsm` — they are
embedded drawing objects in `xl/media`, **not cell values**, so reading the sheet with
`openpyxl(values_only=True)` shows an empty IMAGE column and misses them entirely. Each was matched
to its style by the drawing anchor row in `xl/drawings/drawingN.xml`, so every shot is provably the
one sitting in that workbook row. Filenames are semantic (`belt-lv-multicolor.jpg`,
`backpack-green.jpg`). Nothing renders the "Photo Coming" tile any more.

Left on disk but no longer referenced: `stock-*.jpeg` (old Pexels placeholders) and
`IMG_7806/7807/7817.jpeg` — see the photo discrepancy under Known TODOs before deleting the latter.

**Naming:** listings name the real designer (Amiri, Chrome Hearts, Louis Vuitton, That's An Awful Lot Of
Cough Syrup). This is correct and intentional for a reseller of authentic goods — the first-sale doctrine
and nominative fair use permit using a brand's name to describe genuine items you own and are
reselling, which is why every established resale platform lists by brand. The owner states all stock
is authentic and that receipts are provided to buyers after purchase.

## Features and where they live

| Feature | JS | Notes |
|---|---|---|
| Opening logo reveal | `initIntro` | Full-screen `#intro` at z-index 2000, above every other layer on the page (grain 999, header 100), so it plays before anything is reachable. ~3.3s: the logo spins two full turns on Y while scaling up (2100ms), a viewport-wide shine sweeps across at 950ms, the overlay fades at 2600ms. Behind it sits a city backdrop — two `images/skyline.png` bands tiled `repeat-x` at different scales, opacities and drift speeds for parallax depth, over a blue-lifted night gradient with a horizon glow. Dismissed by the Skip button, a click anywhere, or Esc/Space/Enter. `animationend` unmounts it, with a 4200ms `setTimeout` guard because a backgrounded tab never fires it. Set `INTRO_ONCE_PER_SESSION = true` to play only once per session instead of every load. |
| Featured picks stack | `renderFeatured` / `initStack` | Top 4 items ranked by real visitor clicks, falling back to `DEFAULT_FEATURED_ORDER` |
| Bid of the Week | `renderBidCard` / `refreshBidState` | Auto-rotates weekly via ISO week number — no manual curation. Refreshes on load and on `visibilitychange`, **never on a timer** — see below |
| Catalog | `renderProducts` / `getFilteredProducts` | Category tiles, search, price range, size chips, in-stock toggle, sort |
| Footer / vouches | `initVouchFooter` | The site has exactly **one** footer, fixed to the bottom of the viewport, and it *is* the vouch rotator: one buyer quote at a time, swapped every `VOUCH_ROTATE_MS` (4s), with the brand/copyright line beneath. Quotes come verbatim from the Instagram reference post (`VOUCH_POST_URL`) — **never reword one**, since they are other people's words and editing turns a real quote into a fabricated one. Handles are stored **already masked** in `VOUCHES`: masking only at render would still ship the real usernames in the page source, which is not anonymity. The originals are on the public post. Rotation pauses on hover and while the tab is hidden; `body` carries a `padding-bottom` matching the footer height so content never runs underneath it. |
| Newsletter signup | `initSignup` | Optional `#signup` section near the foot of the page, posting to `/api/subscribe`. **Nothing is gated behind it** — browsing, pricing, cart and checkout all work without it, and a failed POST just says so rather than blocking. Was a full-screen overlay at z-index 1000 that held the store hostage until an email was handed over; the email is now asked for once at checkout, where it is actually needed to send a confirmation. |
| Cart | `loadCart` / `saveCart` | `localStorage["poc_cart"]` stores `[{name, size, qty}]`; a *line* is a product + chosen size + quantity, keyed by `lineId` (`name__size`), so two sizes of one style are two lines. `addToCart` tops up an existing line rather than refusing it, returning `"added"` / `"topped-up"` / `"maxed"`. The badge counts units, not lines. `loadCart` drops lines whose style or size has since left `PRODUCTS`. |
| Checkout | `initCheckout` / `startStripeCheckout` | **Two flows, chosen by the server.** With Stripe configured the button reads *Pay $X* and hands off to Stripe's hosted page; without it, the original reserve-and-DM flow. There are **no card fields either way** — hosted Checkout means card details never touch this site. Only email/items/`ship_to` are POSTed; every price is recomputed server-side. See the Payments section below. |
| My Orders | `initOrders` | Email lookup, no accounts |
| 3D tilt | `initTilt` | Any element with `class="tilt"` and optional `data-tilt-max` |

`initTilt()` must be re-called after injecting new `.tilt` markup (`renderProducts` already does).
Everything is wired up from a single `DOMContentLoaded` handler at the bottom of the file.

## Payments (Stripe Checkout)

**Test mode only, and not deployed.** Full setup, the manual test script and the go-live checklist
are in [STRIPE.md](STRIPE.md); this is the part a future change has to not break.

`python3 tools/stripe_preflight.py` reports what a given machine is missing — key, mode, webhook
secret, return URL, payment columns — and prints the next step for each. It **never prints a
secret** (keys are redacted to mode + last four), so its output is safe to paste into a chat or an
issue, and it exits non-zero unless payments would really be enabled.

`stripe_client.py` talks to Stripe's REST API with `urllib` rather than the `stripe` SDK, so the
project keeps its zero-dependency promise. Form encoding, bracket notation for nested params and
webhook signatures are all implemented there.

**Payments are off unless a secret key AND a webhook secret are both present.** A half-configured
install falls back to the reserve flow, deliberately: an order that can be paid but never confirmed
is worse than one that cannot be paid at all, because the buyer is charged and the shop never
ships. `is_configured()` is the single source of that answer, and `/api/payments/config` is how the
front end learns it.

**A live key is refused unless `POC_ALLOW_LIVE_PAYMENTS=1`.** Pasting a `sk_live_` key is not
enough to start taking real money; someone has to set that variable on the server too. Keys are
never logged whole — `redact()` shows the mode and the last four characters.

Three rules the payment path depends on:

- **The browser never sends a price.** `/api/checkout/session` accepts `name`/`size`/`qty` and
  nothing else that touches money. `orders.create_order()` prices the basket from the database
  ladder and the Checkout Session is built from *those* figures, so a tampered cart changes what is
  ordered, never what it costs. There is a test that posts `price: 0.01` and asserts Stripe is
  still told $21.00.
- **The webhook marks an order paid, not the success page.** The buyer's return to
  `?checkout=success` is cosmetic; `initCheckoutReturn()` asks `/api/checkout/status`, which reads
  the order's real state. Typing that URL by hand proves nothing.
- **A payment is only honoured when the amount matches.** `orders.amount_matches()` compares
  `amount_total` and currency against the order before `mark_paid` runs. A mismatch is logged
  loudly and the order is left pending — never fulfilled on the strength of the event alone.

Idempotency is not reimplemented here: `payment_events` already keys on the provider's event id, so
a redelivered webhook fails that insert and becomes a no-op inside `mark_paid` / `cancel_order` /
`refund_order`. Stripe redelivers after any non-2xx and after an outage, so this matters — there is
a test that replays an event and asserts stock does not move twice.

**An unhandled event type returns 200, not an error.** A non-2xx makes Stripe retry for days.
Genuine handler faults do return 500, because those *should* be retried. A payment arriving for an
order that has left `pending` is in the first category, not the second — retrying can never make a
cancelled order payable — so it returns 200 and logs instead. When that order was cancelled rather
than already paid, the log is deliberately loud: Stripe collected money for stock the shop has
released, and that needs a person, not a retry.

**The Checkout Session is given an `expires_at` equal to `RESERVATION_TTL_SECONDS`.** A session is
payable for 24 hours by default while the reservation lasts 30 minutes, so without it a buyer could
pay most of a day later for units already back on the shelf and possibly resold — money taken,
nothing to ship. The value is passed from the TTL rather than being a constant of its own, so the
window a buyer can pay in and the window stock is held for cannot drift apart. Stripe refuses an
`expires_at` under 30 minutes, which is why a shorter TTL is clamped rather than mirrored.

`set_payment_intent()` is deliberately non-fatal. It only stores the id a later refund event needs;
letting its unique-index violation raise would abort the handler before `mark_paid` ran, so Stripe
would retry a payment that already succeeded and the order would never be marked paid. Money
arriving matters more than an index being tidy.

Copy moves with the flow. `applyPaymentCopy()` swaps the eyebrow, the notice, the trust-strip line,
the button and the disclaimer together, because every one of them makes a promise that is false in
the other mode — "no card details are collected" while Stripe collects them is the same kind of
untrue claim that got "Encrypted checkout" removed. In test mode the panel says so in bold.

## Pricing

Prices live in [pricing.json](pricing.json), **not in code** — the owner edits that file and reloads.
`PRODUCTS` still carries a price literal, but only as the fallback when a product has no entry.

Three levels, each falling back to the next:

- `products[name]` — per-product prices keyed by tier id, plus an optional `tiers` override.
  Keyed by the product's **exact `name`** from `PRODUCTS` (not `fullName`), which is the same key
  the cart and click tracking use. `brand`/`category` in each entry are labels for the human
  editing the file — the real ones live in `script.js`.
- `categories[category].tiers` — the quantity ladder for that category. **This is why the system
  isn't one-size-fits-all**: shirts break at 5/10/20 while shoes break at 3/6/12.
- `defaultTiers` — used by any category with no entry, so a future category prices itself sensibly
  before anyone touches the file. `Bags` and `Shorts` are already defined with no products yet.

`priceFor(p, qty)` is the only thing that answers "what does this cost" — it walks the product's
ladder and returns the highest tier whose `minQty` the quantity reaches *and* which has a price.
A tier with no price is skipped, so a half-filled entry falls to the tier below it rather than to
zero. `tierFor(p, qty)` returns which band applied; `linePrice(line, lines)` prices one cart line.

Conventions the rest of the code depends on:

- **Tier prices are per unit, not a package price.** 5 shirts at `smallBulk: 26` is $130.
- **Quantity is pooled per category, across styles and sizes** — 3 of one tee plus 2 of another is
  5 shirts, and all 5 bill at the 5+ price. `poolUnitsIn(lines, p)` sums every line sharing the
  product's `category`, which is the same unit the tier ladder is defined on. Categories never pool
  into each other: belts in the basket don't move shirts up a tier.
- **The qualifying price applies to every unit in the category**, including a style contributing a
  single piece — 9 of one tee plus 1 of another prices all 10 at the 10+ price.
- **`tierFor` charges the cheapest tier reached, not the deepest.** Since raising quantity only adds
  tiers to the reached set, taking the minimum makes per-unit price mathematically non-increasing in
  quantity — a buyer can never pay more per item by buying more, even if a bulk price is later typed
  in above the retail one. On a correctly descending ladder this picks the same tier either way.
  `validatePricing()` separately warns when a ladder isn't descending, so the data still gets fixed.
- `p.price` is overwritten at load with `priceFor(p, 1)`, so every existing read of `p.price`
  (cards, modal, price filter, sort, the bid's starting price) shows the retail tier without
  needing to know pricing exists. The literal from `PRODUCTS` stays available as `p.basePrice`.
- `loadPricing()` must be awaited **before** anything renders a price — it runs first in the
  `DOMContentLoaded` handler.
- Opening `index.html` over `file://` can't fetch the JSON; it falls back to the `PRODUCTS` prices
  and logs a warning. Prices only work properly through `server.py`.
- `validatePricing()` logs misspelled product keys, non-numeric prices, tier ids that aren't in the
  ladder, and products with no entry. **Check the console after editing pricing.json** — a typo
  degrades quietly to the old price rather than breaking the page, so the warning is the only signal.

Current state: shirts are priced $24.99 / $21 / $18 / $16 across the 1 / 5 / 10 / 20 ladder — the
only category with real bulk pricing. Every other category is still seeded to its original price at
every tier, so bulk is a no-op there until the owner sets numbers.

Note the price cliff this creates: 9 shirts is $189 but 10 shirts is $180, and 19 is $342 while 20
is $320. That's inherent to quantity breaks, not a bug. The cart now calls it out — see the
`is-cliff` nudge above — rather than leaving a buyer to discover it by accident.

`pricing.json` is deliberately **not** in `PRIVATE_FILES` — the browser has to fetch it. That means
wholesale tiers are publicly readable at `/pricing.json`. If bulk pricing should be private, it has
to move behind an API that only returns the retail price to anonymous visitors.

The ladder is surfaced in four places, all reading through `priceFor()` so a promise and a charge
can never drift apart:

- **Card** — `bulkStripHtml(p)`, the discounted tiers only ("5+ $21.00"). Empty for a category whose
  tiers all sit at retail, so it costs nothing to leave on every card.
- **Modal** — `bulkTableHtml(p)`, the full ladder as bands ("1–4 / 5–9 / 10–19 / 20+") plus the retail
  reference row and the mix-and-match note.
- **Cart** — `bulkFeedbackHtml(lines, true)`: a "Bulk discount applied −$X" row from
  `savingsAgainst(lines)`, plus one nudge per category from `nextTierFor(lines, category)`. Each
  cart line shows the retail figure struck through above the charged one, with the tier that
  replaced it named beneath.
- **Checkout** — `bulkFeedbackHtml(items, false)`. Savings only: quantities are locked on the
  payment step, so an offer the buyer can't act on is noise.

Rules the nudge obeys, all of which have a test in the browser console history:

- It only fires when the next band **actually lowers what the current items cost** —
  `nextTierFor` compares `catSubtotalAt(lines, cat, t.minQty)` against today's subtotal, so a
  flat ladder (every category except T-Shirts) produces nothing at all.
- It never suggests buying stock that doesn't exist: `categoryHeadroom()` must cover the shortfall.
  That helper uses `p.stock ?? 1`, **not** `|| 1` — absent means one-off, but an explicit `0` means
  sold out, and `0 || 1` would quietly resurrect a unit.
- When topping up costs *less* than stopping short — the price cliff, e.g. 9 shirts $189 vs 10 for
  $180 — it adds the `is-cliff` styling and spells the comparison out. `cheapestAddCost()` decides
  this by pricing the shortfall at the cheapest in-stock style in that category.
- Category nouns come from `bulkNoun()`, which falls back to the **singularised** category name
  ("Belts" → "belt"); `bulkPlural()` re-pluralises. Set `bulkNoun` in `pricing.json` for anything
  that rule gets wrong.

## Shipping

Rates are weight-based, configured in the "EDIT THIS: shipping" block at the top of
[script.js](script.js). Three pieces:

- `CATEGORY_WEIGHT_OZ` — per-category shipping weight. **These are estimates**, since the workbook
  carries no weights; a product can override with its own `weightOz`. Postage bills on real weight,
  so put the stock on a scale before going live.
  **Its keys must match `categories` in products.json exactly, and it is mirrored in `db/orders.py`**
  — the JS quotes the buyer, the Python charges the card. Renaming a category without renaming its
  key here is silent: the lookup misses, `DEFAULT_WEIGHT_OZ` (8 oz) applies, and nothing complains,
  because a missing key is indistinguishable from "no estimate yet". That happened — `ff30d32`
  renamed T-Shirts→Shirts and Backpacks→Bags and both tables kept the old keys, so **every bag
  shipped billed 8 oz instead of 32**, roughly $3.50–$4.00 of postage per order out of pocket.
  `test_pricing_parity` did not catch it because both copies were wrong in the same way;
  `tests/test_shipping_weights.py` now checks the keys against the real category list as well as
  against each other, and fails on a dead key, a missing one, or the two files disagreeing.
- `SHIPPING_TIERS` gets the same treatment in that file: band edges must land on whole pounds
  (USPS bills at the rounded-up pound, so an edge anywhere else splits one carrier price into two
  of ours), bands must ascend in weight and price, postage must never fall as weight rises, and the
  JS and Python tables must match. Which pounds have no band at all is recorded in
  `KNOWN_MISSING_POUNDS` and checked both ways, so closing the gap is a deliberate edit rather than
  something nobody writes down. That file loads `db/orders.py` **from source, not through
  `__pycache__`** — a .pyc is validated on (mtime, size), and a same-length edit in the same second
  is served from cache, which had the tests checking bytecode instead of the file on disk.
- `PACKAGING_OZ` — mailer/padding, added once per order.
- `SHIPPING_TIERS` — cheapest-first bands; the first one the order's total weight fits under wins,
  falling back to `SHIPPING_OVER_MAX`. The first band is flat for everything under 1 lb because the
  12 July 2026 USPS change made all sub-1lb Ground Advantage the same price within a zone.

**The tier prices are national-average placeholders, not real quotes.** Ground Advantage is
zone-priced, so a flat table overcharges nearby buyers and undercharges distant ones. Replace them
with quotes from Pirate Ship's calculator for the zones actually shipped to.

`shippingFor(lines)` and `orderWeightOz(lines)` drive the cart, the checkout panel, and the stored
order. Format money with `money(n)` so everything reads as two decimals.

**Zone pricing: estimate in the cart, real rate at checkout.** Ground Advantage is priced on weight
AND zone, but the cart has no address — so the cart is labelled *Estimated shipping* and prices off
the flat ladder, and the checkout panel replaces that figure once a ZIP is entered. The checkout
figure comes from `POST /api/shipping/quote`, i.e. **from the server**, computed by the same
`orders.quote()` that prices the order and builds the Stripe session. There is deliberately no
second implementation in the browser: that is how a displayed figure and a charged one drift apart.
`setCheckoutShipping()` is the single writer for the shipping line, the total and the button
amount, so those three can never disagree on screen.

**`zone_pricing_ready()` is all-or-nothing.** It requires a non-empty `zone_map`, every core and
fallback cell in `shipping_rates.json` holding a price, `rate_table_problems()` empty, and every
filled cell backed by a verified `cell_provenance` entry on the table's own service and rate basis.
Until then every order prices off the flat ladder. A partly-filled table would price two identical
baskets by different rules depending on which cell happened to be filled, with nothing on the page
to say so — and a *completely* filled one can still be holding a mistake, which is why soundness and
provenance are part of "ready" rather than a warning printed somewhere.

**The table is one carrier, one service, one rate basis.** `carrier` / `service` / `rate_basis` are
separate fields because "USPS Ground Advantage" as a single string could not express the distinction
that actually bit: **Ground Advantage Cubic is a different product**, priced on the box's volume in
0.1 cu ft increments and flat across weight up to 20 lb (max 22 in any dimension, 1.0 cu ft, 20 lb).
Pirate Ship rate shops Cubic against weight-based automatically and shows whichever is cheaper, so
cubic prices arrive without anyone asking for them — that is where seven quotes at a flat $5.93
across 3–9 lb came from. A volume-priced figure in a weight-indexed band is not a slightly-wrong
price, it is a price for a different variable. See [SHIPPING.md](SHIPPING.md) for what that means for
the whole table's shape.

**`rate_table_problems()` rejects a table that cannot be right**: postage falling as weight rises
(the exact shape a cubic quote makes next to a weight-based one), a nearer zone costing more than a
further one, or an over-max fallback undercutting the band it backs up. Nothing caught any of this
before — a 2 lb cell at $9.15 sat above 3, 4 and 5 lb cells at $5.93 and the code was happy.

**An unquoted pound rounds UP into the table, never out of it.** `zone_rate_cents()` walks
`table_bands()` for the cheapest band at or above the parcel's billable pound. It used to look up
the exact band and, finding none, drop straight to `over_max` — the fallback for parcels heavier than
the whole table and the dearest cell in it. Bands 1, 10, 12, 14 and 15 have no row, so a 10 lb order
paid the over-max price while an 11 lb order paid the 11 lb rate, and a parcel of exactly 16.0 oz
paid it too.

**A zone is never inferred from distance.** `zone_for_zip()` reads `zone_map`, keyed on the
destination's first three digits. USPS publishes the chart per origin; mileage only approximates
it. An unmapped prefix returns None and falls back to the estimate rather than borrowing a
neighbouring zone's price.

**Rate table or rate API?** [SHIPPING.md](SHIPPING.md) has the worked answer. In short: Pirate Ship
has no rating API, Shippo does and fits the urllib pattern at 1¢ per rate — but Shippo's rates are
not Pirate Ship's, so quoting on one and buying labels on the other reintroduces the displayed-versus-
charged drift this codebase refuses everywhere else. Quote and buy on the same provider, or keep both
on Pirate Ship.

## Shipping labels (Pirate Ship)

**Pirate Ship has no API** — that is a deliberate product decision on their end, not a missing
credential, so labels cannot be bought programmatically. Their supported bulk path is spreadsheet
upload with a field-mapping step, and `GET /api/labels.csv` emits exactly that: one row per order
with Name / Email / Address 1 / Address 2 / City / State / Zip / Country / Weight (oz) / Items.
Add `?unshipped=1` to skip orders whose status starts with "Shipped". Orders placed before the
address split are skipped, since they have no label-ready address.

Never ask for or handle the owner's Pirate Ship credentials — they download the CSV and upload it
themselves.

## Admin dashboard

[admin.html](admin.html) at `/admin.html` — a standalone page for managing the catalog. It shares
no CSS or JS with the storefront, so nothing done there can change how a customer sees the site.

**Auth — password, hashed.** The owner sets their own password on first visit; there is no default
and no seeded credential anywhere in the repo. It is stored only as **PBKDF2-HMAC-SHA256, 600,000
iterations, 16-byte random salt** in `admin_auth.json` (mode 0600, gitignored, in `PRIVATE_FILES`).
The plaintext is never written, logged, or returned — it exists only for the moment it is verified.

Login exchanges the password for a **session token**, held in memory server-side with a 12h TTL, so
the deliberately-slow hash runs once per login rather than on every request. Restarting the server
signs everyone out, which is the right default here and avoids a second secret on disk. The
dashboard keeps the *session token* — never the password — in `sessionStorage`.

- `GET  /api/admin/status` — unauthenticated, and deliberately so: the login screen has to know
  whether a password exists before anyone can log in. Returns only that one bit plus any lockout.
- `POST /api/admin/setup` — first password. **Closes permanently once one is set**, so it can't be
  used to take over an existing install.
- `POST /api/admin/login` — password in, session token out.
- `POST /api/admin/password` — needs a live session **and** the current password, so someone at an
  unlocked browser still can't lock the owner out. Signs out every other session on success.
- `POST /api/admin/logout`.

**Brute force:** 8 wrong attempts per client address locks that address out for 15 minutes, and the
correct password is refused during the lockout too. `hmac.compare_digest` on the derived key keeps
a near-miss from being distinguishable by timing. The error is always "Incorrect password." — never
which half was wrong.

Forgotten password: delete `admin_auth.json` and restart. The dashboard returns to its first-run
screen. That is also the only reset, by design — there is no recovery path that doesn't involve
filesystem access to the machine.

**Endpoints** — all admin-only:

- `GET  /api/admin/check` — verify a token without pulling the catalog
- `GET  /api/admin/data` — `{products, pricing}`, both files in one response
- `GET  /api/admin/images` — every file in `images/`, for the picker
- `POST /api/admin/save` — `{products, pricing}`; validates, backs up, then writes both
- `POST /api/admin/upload?name=<filename>` — raw file bytes as the body, saved into `images/`

**Uploads take the raw File as the request body**, not `multipart/form-data` — the browser can post
a `File` directly and the stdlib never has to parse a multipart envelope. `?name=` carries the
original filename, and the response returns the repo-relative path the file was written to.

Three guards, and all of them matter because `images/` is served by the static handler:

- **Extension whitelist** (`.jpg .jpeg .png .webp .gif`) — this is what stops someone with the
  token dropping a `.py` or `.html` file into a directory the server hands out.
- **Magic-byte check** — a shell script renamed to `.jpg` is rejected, so the extension can't be
  the only thing vouching for the content.
- **12MB cap**, enforced before the body is stored.

**Photos are optimized in the browser before upload**, not on the server. A phone photo is 4032px
and several MB; the biggest the storefront ever shows one is a lightbox on a retina screen.
`optimizeImage()` in `admin.html` caps the long edge at `MAX_EDGE` (1600) at `JPEG_QUALITY` (0.85)
— a 4032x3024 / 1797KB shot lands as 1600x1200 / 184KB, a 90% saving.

It runs client-side for a reason: server-side would mean Pillow (the project has zero dependencies)
or `sips` (macOS-only, and this repo is edited from a Windows PC too, where it would silently do
nothing). The browser also gets two things for free that matter for iPhone photos:

- **HEIC decoding.** Safari decodes it natively and the canvas re-encodes to JPEG, so the server
  never sees a format it can't handle. The file picker accepts `.heic/.heif` for that reason.
- **EXIF orientation.** A portrait iPhone photo is stored landscape with a "rotate 90" tag.
  `decodeOriented()` asks for `imageOrientation: "from-image"`, so the pixels are rotated before
  scaling — without that, browsers whose default is `"none"` produce a sideways product shot.

Rules it follows, each with a test in the browser console history:

- **Aspect ratio is one scale factor applied to both edges**, so it can't drift. A 6000x1000
  panorama comes out 1600x267.
- **Never upscales.** A 400x300 image is left at 400x300.
- **Alpha is never flattened onto black.** PNG/GIF/WebP sources go out as WebP (or PNG), only
  photos become JPEG.
- **Animated GIFs pass through untouched** — canvas would keep the first frame and discard the rest.
- **Anything it can't decode is uploaded unchanged** rather than throwing, leaving the server's
  extension and magic-byte checks to make the final call.
- If nothing was resized and the re-encode came out *larger*, the original is kept.

Filenames are reduced to `basename` and stripped to `[A-Za-z0-9._-]`, so `../../etc/evil.png`
lands as `images/evil.png` rather than escaping the directory. An upload never overwrites: a
colliding name gets `-2`, `-3` appended, because another product may still point at the old file.

**products.json and pricing.json are written together or not at all.** They reference each other by
product name; saving one without the other is how a product ends up priced at zero. Every save
timestamps a copy of both into `backups/` (gitignored) first.

**Secrets can't be committed.** `.gitignore` covers `admin_auth.json`, `*.key`, `*.pem`, `.env*`
and `secrets.json`, but `.gitignore` is only a default — `git add -f` walks straight past it. The
real stop is `tools/pre-commit`, which rejects a commit containing any of those paths and also
catches a `"hash"`/`"salt"` pair pasted into a tracked file. **`.git/hooks` is not version
controlled, so cloning does not bring the hook with it** — run `sh tools/install-hooks.sh` once on
each machine, including the Windows PC.

`validate_catalog()` in [server.py](server.py) rejects a bad payload before anything is written —
duplicate ids or names, unknown categories, non-numeric prices, negative or non-integer quantities,
empty size lists, bad status values. The dashboard validates too, but the server is the only thing
between a hand-rolled POST and the catalog, so the rules live in both.

**The quantity ladder is never invented by the dashboard.** Tier inputs are generated from the
product's real ladder in `pricing.json`, so a shirt shows four boxes (1/5/10/20) and keeps all four
on save. Renaming a product moves its pricing entry with it; deleting one removes its entry.

Writes go through `save_json_pretty()`, which writes to a temp file and `os.replace`s it — atomic,
so a crash mid-write can't leave a truncated catalog — and keeps the files indented and diffable.

## Landed cost (foundation)

`costs.json` holds what stock actually costs: supplier price, freight allocation, fees, and the
landed cost that falls out of them. It is **private and never committed** — it is in
`PRIVATE_FILES` (so `/costs.json` 404s) and in `.gitignore`, and `tools/pre-commit` blocks it even
when forced. **The GitHub repo is public and `products.json` is fetched by every visitor**, which
is exactly why cost data does not live in either.

Keyed by the product's **`id`**, not its name, so renaming a product never orphans its costs.

- `itemCost`, `shippingPerUnit`, `extraFeesPerUnit` — per unit, entered by hand for now.
- `shippingMethod` — `air` / `sea` / `other` / `null`.
- `landedCostPerUnit` — **derived**, recomputed by `recompute_landed_costs()` on every save. Never
  hand-edit it; a typed value is overwritten. **A null in any input yields a null total**, not a
  low one: "not entered yet" and "costs nothing" are different, and conflating them misprices
  stock.

`shipments` in the same file is the structure the future allocator will read: name, date, method,
`totalShippingCost`, `totalFees`, `allocationBasis` (`units` / `value` / `weight`) and
`lines[{productId, qty, unitCost}]`. **Nothing is allocated automatically yet** — the per-unit
figures above are still typed in. `allocationBasis` is recorded now so the split is an explicit
choice later rather than an assumption baked into whoever writes the allocator.

`validate_costs()` rejects negative or non-numeric money, unknown shipping methods, duplicate
shipment ids, and a shipment line pointing at a product that no longer exists — that last one
matters because such a line would silently drop its share of the freight when the allocator runs.

The dashboard shows all of this in a **Cost & landed cost** section in the product editor, with the
landed figure computed live and read-only, plus a `Landed` column in the product list. Nothing
cost-related is rendered on the storefront.

## Serving private files

`server.py` uses `SimpleHTTPRequestHandler`, which serves the whole project directory. `PRIVATE_FILES`
blocks the data files (and any dotfile) with a 404 before the static handler ever sees the request —
without it, `orders.json` hands customer names, emails and shipping addresses to anyone who guesses
the URL. **Any new file holding customer data must be added to that set.** Reach order data through
`/api/orders` (scoped to one email) or `/api/labels.csv`.

`stripe_config.json` is in that set too. The webhook signing secret in it is as dangerous as the
API key: anyone holding it can forge a "payment succeeded" event and have the shop ship for free.

## API (server.py)

All responses are JSON; all writes are guarded by one global lock and persisted to gitignored JSON
files in the repo root (`clicks.json`, `bids.json`, `orders.json`, `subscribers.json`).

- `GET  /api/stats` — click counts by product name
- `POST /api/click` — `{name}`, increments
- `GET  /api/bid?item=` / `POST /api/bid` — `{item, amount, name}`, rejects bids at or below current
- `GET  /api/orders?email=` / `POST /api/order` — `{email, items, subtotal, shipping, total, weight_oz, ship_to}`, where `ship_to` is `{name, address1, address2, city, state, zip, country}` split into separate fields so the label CSV can map them
- `GET  /api/labels.csv[?unshipped=1]` — Pirate Ship bulk-upload spreadsheet
- `POST /api/subscribe` — `{email}`, newsletter signups from the `#signup` section
- `GET  /api/payments/config` — `{payments_enabled, mode}`; one bit and a mode, never a key
- `POST /api/checkout/session` — `{email, ship_to, items:[{name,size,qty}]}` → a Stripe Checkout URL
- `POST /api/stripe/webhook` — signature-verified payment events; **this is what marks an order paid**
- `GET  /api/checkout/status?ref=` — order state for the return page, read from the database

This is a dev-grade backend: flat files, no auth, no validation beyond the basics, single process.
Anything real (payments, an admin view, sending mail) needs a proper backend behind it.

## Known TODOs

- **Payments: Stripe Checkout is wired up in test mode and NOT deployed.** What is left is the
  owner's call, not code: supply live keys, set `POC_ALLOW_LIVE_PAYMENTS=1`, register the webhook
  endpoint on the real domain, and approve a deploy. See [STRIPE.md](STRIPE.md). Without keys the
  site still runs the reserve-and-DM flow, and there are still no card fields anywhere — hosted
  Checkout is what keeps it that way. Claims in the checkout copy must stay things the page really
  does; `applyPaymentCopy()` swaps every one of them with the flow.
- **Shipping rates need two fixes before real money** (weights are correct as of `844518a`;
  worksheet in [STRIPE.md](STRIPE.md)). First, the prices are national-average placeholders and
  Ground Advantage is zone-priced, so replace them with Pirate Ship quotes for the zones actually
  shipped to. Second, **`SHIPPING_TIERS` has no 4, 6, 7, 8 or 9 lb band** — it jumps 3→5→10 — and
  USPS bills a parcel at its rounded-up pound, so anything in a missing band pays the next band up.
  A 12-shirt order weighs 5.4 lb and is quoted the **10 lb** rate of $17.00; so are 2 pairs of
  shoes, 3 bags, and 2 bags + 4 shirts. The overcharge lands on the customer rather than the shop,
  which is why nothing looked wrong. Category weights are still estimates too — use a scale.
- Resend: `/api/subscribe` has a TODO for the welcome email and drop announcements; account/API key
  not set up yet.
- **Photo quality and provenance.** The workbook shots max out at ~420px, which is soft for a
  storefront card. They are also clearly supplier/wholesale images rather than own photography:
  `shoe-silver-white.jpg` has size quantities ("42*1 43*1 44*3 45*2") burned into the frame and
  another brand's box visible behind the shoes, and `backpack-green.jpg` carries resale-listing
  zoom-icon chrome in its corners. Own photography would fix the resolution, the overlay text, and
  any third-party copyright question in one pass.
- **Photo discrepancy on the Cough Syrup tees.** `IMG_7806/7807/7817.jpeg` (higher res, ~1200px,
  committed earlier) show a *different* graphic — plain block text — than the workbook photos
  anchored to those same three rows, which show the "PERSONA" graphic. One set is wrong for those
  listings. The workbook photos are wired up because they are row-anchored; needs the owner to say
  which is correct.
- **Brands for Shoes and Backpacks** — the workbook lists only colorway/color for those 11 cards, so
  they render `[brand?]`. Needs the owner to supply them. The green backpack photo shows a Goyard
  chevron pattern and the sneaker photo has a Bottega Veneta box in frame, but neither is confirmed
  — do not write a brand into a listing off a photo without the owner saying so.
- **Pricing** — stock is priced $28–$75, but the stated positioning is designer at plain-tee prices.
  Repricing pass still outstanding. The mechanism is now in place (see Pricing above); every tier is
  seeded to the current price, so the numbers themselves are what's left to decide.
- **Authenticity proof is post-purchase only.** At these prices buyers will assume counterfeit, so
  the site should explain the subsidy and the receipt guarantee up front; nothing on the page does
  that yet.

## Guards that must not be removed

Three defences found missing in a project-wide audit. Each is cheap and each closes a path that a
disabled button alone does not.

- **`addToCart()` refuses a sold-out style itself.** The buttons are disabled for `status: "sold"`,
  but the function had no guard, so a stale page or any future caller walked a sold item into the
  cart. It returns `"unavailable"`; the button-label map must keep an entry for that value or the
  button renders `undefined`.
- **`loadCart()` drops lines whose product is now sold or whose size has no units.** It previously
  dropped only lines whose style or size had vanished, so a cart saved in `localStorage` before a
  sell-out survived and carried an unavailable item to checkout.
- **Deleting a product clears its lines from every shipment.** `validate_costs()` rejects a
  shipment line pointing at an unknown `productId` — correct, but without the dashboard cleaning
  those lines the delete failed with "unknown productId", an error the owner cannot act on. The
  confirm dialog now names the affected shipments, and a failed publish restores them.

- **The webhook verifies signatures and amounts before anything moves.** Either check failing means
  the order stays pending: an unsigned or forged event is a 400, and an amount or currency that
  does not match the order is accepted with a 200 (so Stripe stops retrying) but deliberately not
  acted on. Removing either check turns "someone can POST JSON at your server" into "someone can
  order free merchandise".

**The pre-commit hook needs updating whenever a new secret file is introduced.** It is a fixed list
of filenames, not a rule: `costs.json` was added to the repo and the hook happily committed it
until the pattern was extended. Anything new that holds credentials or business data goes in
`tools/pre-commit`, `.gitignore` and `PRIVATE_FILES` together. `stripe_config.json` was added to
all three when Stripe went in, and the hook also greps for a bare `sk_`/`whsec_` string pasted into
any tracked file.

**Cross-platform:** every path goes through `os.path.join`, there are no shell-outs and no absolute
paths, so the server runs unchanged on Windows. One caveat: `os.chmod(..., 0o600)` on
`admin_auth.json` and `costs.json` is largely a no-op on Windows, so those files rely on the user
profile's own ACLs there rather than POSIX permissions.

## Never poll on a timer

`refreshBidState` used to run on `setInterval(..., 6000)`. On iOS Safari a recurring fetch never
lets the page-load indicator go idle, so the spinner in the address bar turned **forever** on a
phone even though the page had fully rendered and `window.load` had fired. It looked like a broken
site; nothing was broken.

Confirmed by A/B on the LAN — two instrumented copies of this exact build on separate ports, the
only difference being that one line. The spinner stopped only on the build without it.

The bid figure now refreshes on load and on `visibilitychange`. That covers when a viewer can
actually see it change, and coming back to the tab was always the moment that mattered.

**Don't reintroduce a timer that touches the network** — not for bids, stock counts, or anything
else. If live updates ever genuinely matter, the answer is a push (SSE/WebSocket) opened on user
intent, not a poll, and it needs testing on a real iPhone before it ships.

## Mobile performance

Everything expensive about the visual treatment is GPU compositing, not JS or image weight — JS
renders in ~1ms and the product photos are already well compressed (re-encoding them makes them
*larger*; only `logo.png`/`skyline.png` benefited, and those ship as lossless, pixel-identical WebP
with the PNGs kept as fallbacks).

A single `@media (hover: none)` block at the **end of styles.css** turns off the four costly effects
on touch devices. It's gated on hover, not width, so a desktop browser at any window size keeps
everything; a phone gets none of it. What it drops, and why:

- `.noise` — a viewport-sized `mix-blend-mode: overlay` layer forces the whole page to re-composite
  through a blend every frame.
- every `backdrop-filter` — the sticky header and fixed footer re-blur their backdrop on each scroll
  frame. Each background's **opacity steps up** to replace the separation the blur provided, so the
  colour reads the same.
- the skyline drift — the two bands are 192k and 383k pixels of permanently animating layer, so the
  GPU never idles. The city stays put and keeps its opacity; only the motion stops.
- `.tilt` — driven entirely by `mousemove`, which a touch screen never fires, so it renders nothing
  while `will-change` pins all 45 `.tilt` elements onto their own layers. `initTilt()` has a
  matching `(hover: none)` bail-out so the listeners and glow divs aren't created either.

Net effect on a phone: **55 promoted layers / 5.09 megapixels → 8 / 0.56.**

`introOncePerSession()` adds the same gate to the reveal: desktop replays it on every load
(`INTRO_ONCE_PER_SESSION` stays `false`), touch devices play it once per session. The animation
itself is unchanged.

Two more things that are invisible everywhere and apply to both builds:

- **`server.py` caches images.** Blanket `no-store` meant a phone re-downloaded ~424KB of unchanged
  photos on every load. Images now send `no-cache` — kept and revalidated, so they come back as
  bodyless 304s — while markup, code and JSON stay `no-store` so edits appear immediately.
- **Fonts are self-hosted in `fonts/` — the site makes zero external requests.** They came from
  fonts.googleapis.com until a phone on Wi-Fi with no route to Google exposed the cost: that
  stylesheet is render-blocking, so Safari waited out WebKit's stylesheet timeout before painting.
  Measured on the LAN with the font host pointed at an unroutable address, **75,023ms to
  DOMContentLoaded against 31ms** with the request gone. `fonts/fonts.css` holds Google's own
  `@font-face` blocks with the `url()` rewritten — same weights, same `unicode-range`, same
  `font-display: swap` — and is linked **before** `styles.css` so the faces are declared when the
  rules using them parse. Only latin and latin-ext are included; see that file's header for why,
  and re-run the download rather than hand-editing if the weights change.
- Never reintroduce a third-party host on the critical path. A storefront demoed off a laptop has
  to render on a network with no internet at all.

## Style

- Dark, editorial, high-contrast. Space Grotesk + JetBrains Mono, loaded from Google Fonts.
- Colors come from the `:root` custom properties in [styles.css](styles.css:3) — use the tokens,
  don't hardcode hex values.
- Copy voice is lowercase, terse, streetwear ("second hand. first pick.", "gone when it's gone").
- Both CSS and JS honor `prefers-reduced-motion` — keep new animation behind those checks.
- **Responsive header.** The header is one flex row (logo + nav + cart/orders + follow) needing
  ~676px. Under 820px it becomes two rows — identity and actions on top, the section nav on its own
  horizontally scrollable strip — and under 420px the "Follow" wordmark collapses to its icon.
  Before this, a phone viewport pushed the cart and orders buttons off-screen entirely, so they were
  unreachable. Re-check those two buttons stay on-screen after any header change.
- **Cart rows wrap under 560px.** Thumbnail, name, stepper, price and remove on one flex line left
  the product name about 52px wide — one word per line, a ~250px-tall row. `.checkout-item` now
  wraps there, `.checkout-item-info` taking `calc(100% - 56px)` so the controls fall to a second
  line indented past the thumbnail. Re-check a long product name on a phone after touching that row.
- The per-line price is a **column**, not two inline figures: side by side, the struck retail price
  and the charged one nearly double the cell's width, and the row pays for it out of the name.
- The featured stack positions cards with fixed 320px pose math in JS, so `.stack-wrap` is clipped
  under 820px to stop the spread widening the document. Changing `STACK_CARD_WIDTH` means
  re-checking narrow viewports.
- Match the existing idiom: template literals for rendering, `document.getElementById`, section
  banner comments (`// ---------- name ----------`), comments that explain *why*, not *what*.
