# Shipping: rate table or rate API?

The question this answers: should Paid Off Clothes keep filling in a 39-cell table of hand-collected
quotes, or call a carrier API and get the real number every time?

**Short answer: an API is the right destination, but not Pirate Ship's — it doesn't have one — and
switching to Shippo's means moving label buying there too, or the quote and the cost stop matching.**

## What the table costs

The manual table is 39 cells: 13 weight bands x 3 zone groups. Each cell is a quote someone types
into Pirate Ship by hand and copies out. It has to be re-collected every time USPS changes rates,
which is at least annually. Two cells are filled after a week of work, and the effort turned up two
different data-quality failures (a UPS price filed as USPS, and a cubic price filed as weight-based)
that nothing in the pipeline would have caught without a human reading the numbers.

That is the real argument against it. Not the typing — the fact that every cell is a chance to
record the wrong thing, and a static table has no way to notice.

## Pirate Ship

**No API.** This is a product decision on their end, not a missing credential, and it is why
`GET /api/labels.csv` exists: their supported bulk path is spreadsheet upload with a field-mapping
step. There is no rate endpoint, no label endpoint, and no key to apply for.

So "integrate Pirate Ship" is not an option that exists. The choice is between the manual table and
a different provider.

## Shippo

Shippo does have a rating API, and it fits this codebase: REST over HTTPS, so `stripe_client.py`'s
urllib pattern carries over and the zero-dependency promise holds.

Pricing, from [Shippo's API pricing page](https://goshippo.com/pricing/api):

| | API Starter |
|---|---|
| Rate generation | **1¢ per rate** |
| Labels | 30/month free, then **7¢** |
| US address validation | 2¢ |
| Tracking | 2¢ |

At this shop's volume that is a rounding error — but *rate generation is billed per call*, and the
checkout panel currently re-quotes on a debounced ZIP input. Every ZIP a visitor types would spend a
cent. Any integration has to cache by (weight band, ZIP prefix), which is the same shape as the
table, just filled automatically and expired on a TTL rather than by hand.

### The catch, and it is the whole decision

**Shippo's rates are not Pirate Ship's rates.** Pirate Ship bills at USPS Connect eCommerce pricing,
a commercial tier below standard Commercial Pricing. Shippo quotes its own negotiated rates. Quoting
from Shippo while buying labels on Pirate Ship means the figure shown to the buyer is not the figure
the shop pays — in some bands the shop keeps the difference, in others it eats it, and which is
which changes whenever either provider renegotiates.

That is precisely the failure this codebase already refuses elsewhere: there is deliberately no
second shipping implementation in the browser, because "that is how a displayed figure and a charged
one drift apart." Quoting on one provider's rates and buying on another's is the same bug with a
company boundary through the middle of it.

So there are two coherent positions and one incoherent one:

- **Coherent:** keep quoting from a table of Pirate Ship quotes, and keep buying on Pirate Ship.
- **Coherent:** quote from Shippo's API *and buy labels through Shippo*. Quote equals cost by
  construction, labels stop being a CSV upload, and the table disappears.
- **Incoherent:** quote from Shippo, buy on Pirate Ship.

### What moving to Shippo would actually cost

Not the 7¢. The loss is the Connect eCommerce discount, if Pirate Ship is genuinely cheaper for this
shop's parcels — which is a measurable question, not a theoretical one. Quote the same parcel on
both and compare. If Shippo is within a few cents, the automation is worth it. If Pirate Ship is
meaningfully cheaper on the bands this shop actually ships, the discount may be worth more than the
table costs to maintain.

## Cubic changes this argument — and then settles it

Every quote collected up to this point assumed postage is a function of weight. For this inventory
it isn't, and the screenshots prove it rather than suggesting it:

| Weight | Service Pirate Ship showed | Price |
|---|---|---|
| 3–5 lb | USPS Ground Advantage (weight-based) | $5.93 |
| 6–9 lb | USPS Ground Advantage **Cubic** | $8.56 |

Same 12 × 19 × 3 box throughout. Cubic ignores weight entirely, so for that box Cubic is $8.56 at
*every* weight in the table. Pirate Ship rate shops the two and displays the winner, so what the
screen shows is `min(weight_based(lb), cubic(box))` — weight-based wins while it is under $8.56, and
Cubic wins once it is over. **The crossover sits between 5 and 6 lb.**

That is the shop's real cost model. It is not one service, and no single-service table can express
it.

But notice what it *is*: for a fixed box, `min(weight_based(lb), cubic(box))` is still a function of
weight alone. The box is the only other variable, and it is one the shop controls.

## Recommendation: fix the box, keep the table, skip the API

**Do not build a Shippo integration.** There is no existing one — the repo has zero references to
Shippo, EasyPost or any carrier API, so this would be new code, a new account, a new key to keep out
of git, and a per-rate charge on a debounced ZIP field. And it would still quote rates the shop
doesn't pay, because labels are bought on Pirate Ship.

Instead, make the table say what it actually holds:

1. **Pick one standard package** and measure it. One poly mailer for small orders, one box for
   everything else, if two are genuinely needed — but each one is a fixed, measured size, not an
   estimate typed in per quote.
2. **Set `rate_basis` to `cheapest_available`** and declare that package in `quoted_for_package`.
   The code now enforces this: a rate-shopped table refuses to switch on without measured
   dimensions, and refuses any cell quoted for a different box.
3. **Fill each cell with whatever Pirate Ship shows as cheapest** for that package at that weight —
   Cubic or weight-based, whichever wins. No reinterpretation, no judgement call: copy the number on
   the screen and record which service it came from.

Why this beats the API for this shop:

- **Quote equals cost by construction.** Same provider, same package, same rate shopping.
- **No new dependency, no key, no per-call billing.** The zero-dependency promise holds.
- **Fewer quotes than feared.** Once Cubic caps the ladder, every band above the crossover is the
  same number. One quote covers the whole plateau per zone group, so the upper half of the table
  fills itself.
- **It is already built.** The table, the zone map, the validators and the all-or-nothing gate all
  exist and are tested.

The API becomes the right answer only if the shop stops using a standard package — if box size
varies per order, volume stops being a constant, and nothing but a live lookup can price it.

## What is still true about the providers

Pirate Ship has no rating API, so "integrate Pirate Ship" is not an option that exists. Shippo has
one at 1¢ per rate and 7¢ per label after 30 free per month, and it fits the urllib pattern — but
its rates are its own, not the USPS Connect eCommerce tier Pirate Ship bills at. Quoting on Shippo
while buying on Pirate Ship reintroduces the displayed-versus-charged drift this codebase refuses
everywhere else, with a company boundary through the middle of it. If the API route is ever taken,
labels move to the same provider or it isn't worth taking.

Nothing here is wired up. `shipping_rates.json` still drives nothing — `zone_pricing_ready()` is
false and the flat estimate ladder in `script.js` and `db/orders.py` is what the site uses.

Sources: [Pirate Ship Ground Advantage Cubic](https://www.pirateship.com/usps/ground-advantage-cubic),
[Shippo API pricing](https://goshippo.com/pricing/api),
[Pirate Ship commercial pricing](https://www.pirateship.com/usps/commercial-pricing).
