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

## Cubic changes this argument

Every quote collected so far assumed postage is a function of weight. It isn't, for this inventory:
USPS Ground Advantage Cubic is priced on the box's volume and is flat across weight up to 20 lb, and
Pirate Ship rate shops it against weight-based automatically. That is where the $5.93-at-3-4-and-5-lb
figures came from.

If cubic is the cheaper product for these parcels — and at 0.4 cu ft it appears to be by a wide
margin — then **a weight-indexed table is the wrong shape regardless of who fills it in**. Price
follows the box. Two orders of identical weight in different boxes cost different amounts, and the
current model has no dimension input at all: `orderWeightOz(lines)` returns ounces and nothing else.

Expressing cubic by hand means either committing to a fixed box per order profile (so volume becomes
a constant and the table works again), or adding a packing model that picks a box from the basket.
An API sidesteps it: send the real dimensions, get the real cheapest rate, including cubic.

**This is the strongest argument for the API, and it is an argument that only appeared because the
manual quotes were collected carefully enough to contradict each other.**

## Recommendation

1. **Do not collect the other 37 quotes yet.** They would be quotes for a weight model that may be
   the wrong model.
2. **Settle the packaging question first.** What boxes and mailers does this shop actually use? That
   single answer decides whether cubic applies, and therefore whether a weight table can work at all.
3. **Then compare Shippo against Pirate Ship on the same parcel**, once, before committing either
   way. Automation that quotes the wrong price is worse than a table that quotes the right one.

Nothing here is wired up. `shipping_rates.json` still drives nothing — `zone_pricing_ready()` is
false and the flat estimate ladder in `script.js` and `db/orders.py` is what the site uses.

Sources: [Pirate Ship Ground Advantage Cubic](https://www.pirateship.com/usps/ground-advantage-cubic),
[Shippo API pricing](https://goshippo.com/pricing/api),
[Pirate Ship commercial pricing](https://www.pirateship.com/usps/commercial-pricing).
