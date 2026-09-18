// ---------- product data ----------
// Stock lives in products.json, not here. That file is the single source of truth for names,
// brands, photos, sizes, per-size quantities, status and featured flags — the shape an admin
// dashboard will read and write. Nothing about inventory is hardcoded in this file any more.
//
// PRODUCTS stays a `const` array that gets FILLED rather than reassigned: renderProducts,
// getBidItem, computeFeatured, loadCart and the pricing helpers all close over this binding, so
// replacing it would leave half the site pointing at a stale array.

const NEEDS_BRAND = "[brand?]";

const PRODUCTS = [];
let CATEGORIES = ["All"];
let PRODUCTS_LOADED = false;

// Card shape expected by the rest of the file. Built once per product at load: zero-qty sizes are
// dropped for display (they stay in products.json so they can be restocked), `meta` is the card's
// sub-line, and `stock` is the total behind "N left".
function productFromRecord(r) {
  const sizes = (r.sizes || []).filter((s) => Number(s.qty) > 0).map((s) => ({ size: s.size, qty: Number(s.qty) }));
  if (sizes.length === 0) return null;
  const oneSize = sizes.length === 1 && sizes[0].size === "One Size";
  return {
    id: r.id,
    brand: r.brand || NEEDS_BRAND,
    name: r.name,
    category: r.category,
    meta: oneSize ? "One Size" : sizes.map((s) => s.size).join(" · "),
    price: Number(r.retailPrice),
    status: r.status === "sold" ? "sold" : "available",
    sizes,
    stock: sizes.reduce((n, s) => n + s.qty, 0),
    desc: r.description || "",
    featured: r.featured === true,
    // Kept for the pricing fallback and for a future admin UI; pricing.json still wins where set.
    bulkPrice: typeof r.bulkPrice === "number" ? r.bulkPrice : null,
    bulkMinQty: typeof r.bulkMinQty === "number" ? r.bulkMinQty : null,
    images: Array.isArray(r.images) ? r.images : [],
    ...(r.image ? { img: r.image } : {}),
  };
}

async function loadProducts() {
  try {
    const res = await fetch("products.json", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const doc = await res.json();
    const records = Array.isArray(doc) ? doc : doc.products || [];

    PRODUCTS.length = 0;
    records.forEach((r) => {
      const p = productFromRecord(r);
      if (p) PRODUCTS.push(p);
    });

    const cats = Array.isArray(doc.categories) && doc.categories.length
      ? doc.categories.slice()
      : ["All", ...new Set(PRODUCTS.map((p) => p.category))];
    CATEGORIES = cats[0] === "All" ? cats : ["All", ...cats];

    PRODUCTS_LOADED = true;
    validateProducts(records);
  } catch (err) {
    console.error(
      `[products] couldn't load products.json (${err.message}). The catalog will be empty. ` +
      `Run the site through server.py — a file:// page can't fetch it.`
    );
  }
}

// Same idea as validatePricing(): a bad edit degrades quietly, so the console warning is the only
// signal the owner gets. Check it after editing products.json.
function validateProducts(records) {
  const seenId = new Set(), seenName = new Set();
  records.forEach((r) => {
    if (!r.id || !r.name) return console.warn(`[products] record missing id or name:`, r);
    if (seenId.has(r.id)) console.warn(`[products] duplicate id "${r.id}" — ids must be unique`);
    if (seenName.has(r.name)) console.warn(`[products] duplicate name "${r.name}" — the cart and pricing.json key on name`);
    seenId.add(r.id); seenName.add(r.name);
    if (!CATEGORIES.includes(r.category)) console.warn(`[products] "${r.name}" has category "${r.category}", which isn't in the categories list`);
    if (typeof r.retailPrice !== "number") console.warn(`[products] "${r.name}" has a non-numeric retailPrice`);
    if (r.bulkPrice !== null && typeof r.bulkPrice === "number" && r.bulkPrice > r.retailPrice) {
      console.warn(`[products] "${r.name}" bulkPrice ${r.bulkPrice} is above retailPrice ${r.retailPrice}`);
    }
    if (!(r.sizes || []).length) console.warn(`[products] "${r.name}" has no sizes`);
  });
  const live = PRODUCTS.length, total = records.length;
  if (live < total) console.info(`[products] ${total - live} product(s) hidden — every size is at qty 0`);
}

// ---------- EDIT THIS: shipping ----------
// Per-item shipping weights. The workbook carries no weights, so these are estimates by category
// — correct them against a kitchen scale before going live, since postage is billed on real weight.
// A single product can override its category with a `weightOz` field.
//
// KEYS MUST MATCH `categories` IN products.json EXACTLY. They did not for a while: the rename in
// ff30d32 turned "T-Shirts" into "Shirts" and "Backpacks" into "Bags", both lookups started missing,
// and every shirt and bag quietly billed at the 8 oz fallback — trivial for a shirt, a real postage
// loss on a 32 oz bag. Nothing failed loudly because a missing key is indistinguishable from
// "no estimate yet". tests/test_shipping_weights.py now fails if a declared category has no entry.
const CATEGORY_WEIGHT_OZ = {
  Shirts: 7,
  Belts: 10,
  Shoes: 40,
  Bags: 32,
  // No stock in these two yet. Listed anyway so the first product added to one is not silently
  // quoted at the fallback weight — these are estimates like the rest, and need a scale.
  Shorts: 9,
  Tracksuits: 28,
};

// Mirrors DEFAULT_WEIGHT_OZ in db/orders.py. Named rather than inlined so the parity test can
// find it: the two files drifting apart on a weight is exactly the bug that billed every bag at
// 8 oz instead of 32, and an unnamed literal is invisible to the check that now catches it.
const DEFAULT_WEIGHT_OZ = 8;

const PACKAGING_OZ = 3; // mailer / box / padding added once per order

// USPS Ground Advantage tiers, cheapest first; the first tier the order fits under wins.
// As of the 12 July 2026 USPS change, everything under 1 lb costs the same within a zone, which is
// why the first band is flat rather than an ounce-by-ounce ladder.
//
// THESE ARE NATIONAL-AVERAGE PLACEHOLDERS, NOT YOUR RATES. Ground Advantage is zone-priced, so a
// flat table overcharges nearby buyers and undercharges distant ones. Replace each `price` with a
// real quote from Pirate Ship's calculator for the zones you actually ship to.
const SHIPPING_TIERS = [
  { maxOz: 15.99, price: 5.5, label: "Under 1 lb" },
  { maxOz: 16, price: 7.61, label: "1 lb" },
  { maxOz: 32, price: 8.5, label: "2 lb" },
  { maxOz: 48, price: 9.5, label: "3 lb" },
  { maxOz: 80, price: 12, label: "5 lb" },
  { maxOz: 160, price: 17, label: "10 lb" },
];

const SHIPPING_OVER_MAX = 22; // anything heavier than the last tier

function weightOf(p) {
  return p.weightOz ?? CATEGORY_WEIGHT_OZ[p.category] ?? DEFAULT_WEIGHT_OZ;
}

// Total billable weight for an order: every unit, plus packaging once.
function orderWeightOz(lines) {
  if (lines.length === 0) return 0;
  return lines.reduce((oz, l) => oz + weightOf(l) * l.qty, 0) + PACKAGING_OZ;
}

function shippingFor(lines) {
  if (lines.length === 0) return 0;
  const oz = orderWeightOz(lines);
  const tier = SHIPPING_TIERS.find((t) => oz <= t.maxOz);
  return tier ? tier.price : SHIPPING_OVER_MAX;
}

// ---------- pricing ----------
// Prices live in pricing.json, NOT here — the whole point is that they can be changed without
// touching code. This section only loads that file and answers "what does this cost at qty N".
//
// Shape: every category carries its own tier ladder (so shoes can break at 3/6/12 while shirts
// break at 5/10/20), and every product carries a price per tier id. A product may override the
// ladder too. Anything missing falls back: product tiers -> category tiers -> defaultTiers, and
// product price -> the next tier down -> the price hardcoded in PRODUCTS above.
let PRICING = null;

// The price literals in PRODUCTS stay reachable as `basePrice` after pricing.json overwrites
// `price`, so a missing or malformed entry degrades to the old price instead of to NaN.
function stashBasePrices() {
  PRODUCTS.forEach((p) => { p.basePrice = p.price; });
}

// Single-tier stand-in used when pricing.json can't be read — e.g. opening index.html over
// file://, where fetch of a local file is blocked. The site keeps its current prices.
function fallbackPricing() {
  return {
    defaultTiers: [{ id: "retail", label: "Single", minQty: 1 }],
    categories: {},
    products: Object.fromEntries(PRODUCTS.map((p) => [p.name, { prices: { retail: p.basePrice } }])),
  };
}

function normalizeTiers(tiers) {
  return tiers
    .filter((t) => t && typeof t.minQty === "number" && t.id)
    .slice()
    .sort((a, b) => a.minQty - b.minQty);   // ascending, so the last match is the best tier reached
}

async function loadPricing() {
  stashBasePrices();
  try {
    const res = await fetch("pricing.json", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const doc = await res.json();
    PRICING = {
      defaultTiers: normalizeTiers(doc.defaultTiers || []),
      categories: doc.categories || {},
      products: doc.products || {},
    };
    Object.values(PRICING.categories).forEach((c) => {
      if (Array.isArray(c.tiers)) c.tiers = normalizeTiers(c.tiers);
    });
    Object.values(PRICING.products).forEach((p) => {
      if (Array.isArray(p.tiers)) p.tiers = normalizeTiers(p.tiers);
    });
    if (PRICING.defaultTiers.length === 0) PRICING.defaultTiers = [{ id: "retail", label: "Single", minQty: 1 }];
    validatePricing();
  } catch (err) {
    console.warn(`[pricing] couldn't load pricing.json (${err.message}) — falling back to the prices in script.js.`);
    PRICING = fallbackPricing();
  }
  applyRetailPrices();
}

// The tier ladder that applies to one product, most specific first.
// A product carrying its own bulkPrice/bulkMinQty in products.json but with no pricing.json entry
// prices itself off that pair. Lets a product added through an admin dashboard price correctly
// without a second edit to pricing.json; pricing.json still wins wherever it has an entry, so
// nothing that file already covers changes.
function ownTiers(p) {
  if (PRICING.products[p.name]) return null;
  if (typeof p.bulkPrice !== "number" || typeof p.bulkMinQty !== "number") return null;
  return [{ id: "retail", label: "Single", minQty: 1 }, { id: "bulk", label: "Bulk", minQty: p.bulkMinQty }];
}

function ownPrices(p) {
  return { retail: p.basePrice ?? p.price, bulk: p.bulkPrice };
}

function tiersFor(p) {
  const own = ownTiers(p);
  if (own) return own;
  const entry = PRICING.products[p.name];
  if (entry && Array.isArray(entry.tiers) && entry.tiers.length) return entry.tiers;
  const cat = PRICING.categories[p.category];
  if (cat && Array.isArray(cat.tiers) && cat.tiers.length) return cat.tiers;
  return PRICING.defaultTiers;
}

// The tier the buyer actually pays at this quantity, or null if no reached tier has a price.
//
// This picks the CHEAPEST reached tier, not the deepest one. Since raising the quantity can only
// add tiers to the reached set and never remove one, taking the minimum makes the per-unit price
// mathematically non-increasing as quantity rises — a buyer can never pay more per item by buying
// more, even if someone later fat-fingers a bulk price above the retail one. With a correctly
// descending ladder this picks exactly the same tier the deepest-match rule would.
function tierFor(p, qty = 1) {
  const prices = ownTiers(p) ? ownPrices(p) : ((PRICING.products[p.name] || {}).prices || {});
  let hit = null;
  for (const t of tiersFor(p)) {
    if (qty < t.minQty || typeof prices[t.id] !== "number") continue;
    // `<=` so that on a tie the deeper tier wins and gets reported as the one that applied.
    if (hit === null || prices[t.id] <= prices[hit.id]) hit = t;
  }
  return hit;
}

// Per-unit price at this quantity. Everything that shows or sums money goes through here.
function priceFor(p, qty = 1) {
  const prices = ownTiers(p) ? ownPrices(p) : ((PRICING.products[p.name] || {}).prices || {});
  const tier = tierFor(p, qty);
  return tier ? prices[tier.id] : (p.basePrice ?? p.price);
}

// Units that count toward a product's bulk tier. Pooled by CATEGORY: every shirt in the basket
// counts toward every shirt's tier, across styles and sizes alike, so 3 of one tee plus 2 of
// another is 5 shirts and each of the 5 bills at the 5+ price. Tiers are defined per category, so
// the category is the natural unit to count over.
function poolUnitsIn(lines, p) {
  return lines.reduce((n, l) => (l.category === p.category ? n + l.qty : n), 0);
}

// Price for one cart line, judged against the whole basket so the rest of the category counts.
function linePrice(line, lines) {
  return priceFor(line, lines ? poolUnitsIn(lines, line) : line.qty);
}

// ---------- bulk pricing display ----------
// The tiers worth advertising: the ones that actually beat the single-unit price. Read straight
// out of pricing.json through priceFor(), so what a card promises and what the cart charges can
// never drift apart — and so this stays correct when the owner edits prices.
//
// It also means the display needs no category check. A category whose tiers all sit at the retail
// price (every category except shirts today) yields an empty list and renders nothing, and the day
// belts get real bulk pricing their cards pick it up on their own.
function savingTiers(p) {
  const single = priceFor(p, 1);
  const out = [];
  tiersFor(p).forEach((t) => {
    const price = priceFor(p, t.minQty);
    // Skip a tier that saves nothing, and skip a deeper tier that repeats the price above it.
    if (price < single && !out.some((x) => x.price === price)) out.push({ minQty: t.minQty, price });
  });
  return out;
}

// "1–4 / 5–9 / 10–19 / 20+" — each band runs up to the unit before the next one starts.
function bulkBands(p) {
  const tiers = savingTiers(p);
  if (tiers.length === 0) return [];
  const bands = [{ label: `1–${tiers[0].minQty - 1}`, price: priceFor(p, 1), isRetail: true }];
  tiers.forEach((t, i) => {
    const next = tiers[i + 1];
    bands.push({ label: next ? `${t.minQty}–${next.minQty - 1}` : `${t.minQty}+`, price: t.price });
  });
  return bands;
}

// Quantities pool across a whole category, so the note names the category's own noun. Set
// `bulkNoun` per category in pricing.json; falls back to the category name.
function bulkNoun(p) {
  const cat = PRICING.categories[p.category] || {};
  if (cat.bulkNoun) return cat.bulkNoun;
  // Category names are plural; every use of this is singular or gets pluralised by the caller, so
  // "Belts" has to fall back to "belt" or the nudge reads "beltss". Set `bulkNoun` in pricing.json
  // for anything this rule gets wrong.
  const name = p.category.toLowerCase();
  return name.endsWith("s") ? name.slice(0, -1) : name;
}

function bulkPlural(p, n) {
  const noun = bulkNoun(p);
  return n === 1 ? noun : `${noun}s`;
}

function bulkNote(p) {
  return `Mix &amp; match ${bulkNoun(p)} styles — quantity discounts apply automatically in cart.`;
}

// Compact one-liner for the card. Deliberately just the discounted tiers, not the retail row —
// the retail price is already sitting right above it, and repeating it crowds the card.
function bulkStripHtml(p) {
  const tiers = savingTiers(p);
  if (tiers.length === 0 || p.status === "sold") return "";
  return `
        <div class="bulk-strip">
          <span class="bulk-strip-label">Buy more &amp; save</span>
          <span class="bulk-strip-tiers">
            ${tiers.map((t) => `<span class="bulk-tier"><b>${t.minQty}+</b>${money(t.price)}</span>`).join("")}
          </span>
        </div>`;
}

// Full ladder for the detail modal, where there's room for the retail row and the note.
function bulkTableHtml(p) {
  const bands = bulkBands(p);
  if (bands.length === 0 || p.status === "sold") return "";
  return `
    <div class="bulk-table" role="table" aria-label="Quantity pricing">
      <span class="bulk-table-head">Buy more &amp; save</span>
      ${bands.map((b) => `
        <span class="bulk-row${b.isRetail ? " is-retail" : ""}" role="row">
          <span class="bulk-row-qty" role="cell">${b.label}</span>
          <span class="bulk-row-price" role="cell">${money(b.price)} <i>each</i></span>
        </span>`).join("")}
      <span class="bulk-note">${bulkNote(p)}</span>
    </div>`;
}

// ---------- cart tier feedback ----------
// The card and the modal advertise the ladder before the buyer commits; these answer the question
// the cart raises instead — "why did my per-unit price just change?" — and flag the case where one
// more unit costs less than stopping short. All of it reads through priceFor(), so it can't promise
// a number the checkout won't charge.

function catLines(lines, category) {
  return lines.filter((l) => l.category === category);
}

function catUnits(lines, category) {
  return catLines(lines, category).reduce((n, l) => n + l.qty, 0);
}

// What this category's existing lines would cost if the pool held `units`. Each line is priced
// through its own ladder rather than multiplying one price out, since two styles in a category can
// sit at different prices (belts run $45–$60) and only their tier structure is shared.
function catSubtotalAt(lines, category, units) {
  return catLines(lines, category).reduce((s, l) => s + priceFor(l, units) * l.qty, 0);
}

// Total kept back versus paying the single-unit price for every item in the basket.
function savingsAgainst(lines) {
  return lines.reduce((s, l) => s + (priceFor(l, 1) - linePrice(l, lines)) * l.qty, 0);
}

// Units of a category still on the shelf beyond what the basket already holds. The nudge has to
// clear this or it would suggest buying a piece that doesn't exist.
function categoryHeadroom(lines, category) {
  // `??` not `||`: an absent `stock` means a one-off (1 unit), but an explicit 0 means none left,
  // and `0 || 1` would quietly turn a sold-out style back into a purchasable unit.
  const shelf = PRODUCTS.reduce(
    (n, p) => (p.category === category && p.status !== "sold" ? n + (p.stock ?? 1) : n),
    0
  );
  return shelf - catUnits(lines, category);
}

// The nearest tier above the current pool that actually lowers what these items cost. Returns null
// when the category has no ladder, is already at the deepest tier, or the next band saves nothing.
function nextTierFor(lines, category) {
  const units = catUnits(lines, category);
  if (units === 0) return null;
  const sample = catLines(lines, category)[0];
  const current = catSubtotalAt(lines, category, units);
  let best = null;
  // Ladders are defined per category, so any line in it reports the same bands.
  tiersFor(sample).forEach((t) => {
    if (t.minQty <= units) return;
    const projected = catSubtotalAt(lines, category, t.minQty);
    if (projected < current && (best === null || t.minQty < best.minQty)) {
      best = { minQty: t.minQty, need: t.minQty - units, units, current, projected };
    }
  });
  return best;
}

// Cheapest a buyer could add `need` more units of a category for, at the tier they'd land on. Used
// only to test the price cliff — whether topping up genuinely costs less than stopping short.
function cheapestAddCost(category, units, need) {
  const inStock = PRODUCTS.filter((p) => p.category === category && p.status !== "sold");
  if (inStock.length === 0) return Infinity;
  return Math.min(...inStock.map((p) => priceFor(p, units))) * need;
}

function nudgeHtml(lines, category) {
  const next = nextTierFor(lines, category);
  if (next === null) return "";
  if (categoryHeadroom(lines, category) < next.need) return "";

  const sample = catLines(lines, category)[0];
  const addCost = cheapestAddCost(category, next.minQty, next.need);
  // The cliff: topping up to the next band costs no more than the smaller order does today.
  const cliff = next.projected + addCost <= next.current;

  return `
        <div class="bulk-nudge${cliff ? " is-cliff" : ""}">
          <span class="bulk-nudge-icon" aria-hidden="true">${cliff ? "&darr;" : "+"}</span>
          <span class="bulk-nudge-text">
            <b>${next.need} more ${bulkPlural(sample, next.need)}</b> unlock${next.need === 1 ? "s" : ""} the ${next.minQty}+ price &mdash;
            your ${next.units} ${bulkPlural(sample, next.units)} drop${next.units === 1 ? "s" : ""} from ${money(next.current)} to ${money(next.projected)}.
            ${cliff ? `<em>${next.minQty} would cost ${money(next.projected + addCost)} &mdash; less than the ${next.units} you have now.</em>` : ""}
          </span>
        </div>`;
}

// Savings line plus a nudge per category in the basket. The checkout panel passes withNudge:false —
// quantities are locked once you're on the payment step, so an offer you can't act on is noise.
function bulkFeedbackHtml(lines, withNudge = true) {
  if (lines.length === 0) return "";
  const parts = [];
  const saved = savingsAgainst(lines);
  // Half a cent of float dust shouldn't render a "$0.00 saved" row.
  if (saved > 0.005) {
    parts.push(`
        <div class="bulk-saved">
          <span>Bulk discount applied</span>
          <span class="bulk-saved-amount">&minus;${money(saved)}</span>
        </div>`);
  }
  if (withNudge) {
    [...new Set(lines.map((l) => l.category))].forEach((cat) => parts.push(nudgeHtml(lines, cat)));
  }
  return parts.join("");
}

// Cards, the modal, filters, sorting and the bid all read `p.price`. Pointing that at the qty-1
// price keeps pricing.json authoritative for the retail number without touching those call sites.
function applyRetailPrices() {
  PRODUCTS.forEach((p) => { p.price = priceFor(p, 1); });
}

// Typos in a hand-edited JSON file shouldn't fail silently — say so in the console instead.
function validatePricing() {
  const known = new Set(PRODUCTS.map((p) => p.name));
  const problems = [];

  Object.keys(PRICING.products).forEach((name) => {
    if (!known.has(name)) problems.push(`"${name}" isn't a product in script.js — check the spelling.`);
  });

  PRODUCTS.forEach((p) => {
    const entry = PRICING.products[p.name];
    if (!entry) {
      problems.push(`"${p.name}" has no pricing entry — using its script.js price of $${p.basePrice}.`);
      return;
    }
    const prices = entry.prices || {};
    const ids = tiersFor(p).map((t) => t.id);
    if (typeof prices[ids[0]] !== "number") {
      problems.push(`"${p.name}" has no price for its first tier ("${ids[0]}") — falling back to $${p.basePrice}.`);
    }
    Object.entries(prices).forEach(([id, val]) => {
      if (typeof val !== "number" || !(val >= 0)) problems.push(`"${p.name}" tier "${id}" is ${JSON.stringify(val)}, not a number.`);
      else if (!ids.includes(id)) problems.push(`"${p.name}" prices tier "${id}", which isn't in its ladder (${ids.join(", ")}).`);
    });

    // A bulk tier priced above a shallower one is always a mistake — buying more should never cost
    // more per unit. tierFor() refuses to charge it, but the file still needs correcting.
    let cheapest = Infinity;
    ids.forEach((id) => {
      const val = prices[id];
      if (typeof val !== "number") return;
      if (val > cheapest) {
        problems.push(`"${p.name}" tier "${id}" ($${val}) costs more per unit than a smaller quantity ($${cheapest}) — buyers are charged $${cheapest} instead.`);
      }
      cheapest = Math.min(cheapest, val);
    });
  });

  if (problems.length) console.warn("[pricing] " + problems.length + " issue(s) in pricing.json:\n - " + problems.join("\n - "));
  return problems;
}

function money(n) {
  return `$${n.toFixed(2)}`;
}

const state = { category: "All", search: "", sizes: [], minPrice: null, maxPrice: null, inStockOnly: false, sort: "newest" };
const tiltBound = new WeakSet();
let currentModalProduct = null;
let selectedSize = null;
let selectedQty = 1;
let checkoutItems = [];

// Most placeholder pieces are one-off (no stock field, implied qty 1); real multi-unit
// stock sets p.stock explicitly.
function stockLabel(p) {
  const qty = p.stock || 1;
  return qty > 1 ? `${qty} left` : "1 left";
}

// Brand + name as one string, for cart lines, orders, and anywhere a single label is needed.
// An unfilled brand is left off entirely — the `[brand?]` sentinel is a storefront warning, and
// has no business being written into a stored order record.
function fullName(p) {
  return hasBrand(p) ? `${p.brand} ${p.name}` : p.name;
}

function hasBrand(p) {
  return p.brand && p.brand !== NEEDS_BRAND;
}

// Every size a style still has units in, e.g. ["S","M","L","XL"].
function sizesOf(p) {
  return p.sizes.map((s) => s.size);
}

// ---------- bid of the week ----------
let bidItem = null;

function isoWeekNumber(date) {
  const d = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
  const dayNum = d.getUTCDay() || 7;
  d.setUTCDate(d.getUTCDate() + 4 - dayNum);
  const yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
  return Math.ceil(((d - yearStart) / 86400000 + 1) / 7);
}

// Rotates automatically each week — no manual curation needed.
function getBidItem() {
  const eligible = PRODUCTS.filter((p) => p.status === "available");
  if (eligible.length === 0) return null;
  return eligible[isoWeekNumber(new Date()) % eligible.length];
}

function renderBidCard() {
  const card = document.getElementById("bid-card");
  bidItem = getBidItem();

  if (!bidItem) {
    card.innerHTML = `<p class="cart-empty-note">Nothing eligible for bidding right now — check back once new stock drops.</p>`;
    return;
  }

  card.innerHTML = `
    <div class="bid-card-media">${bidItem.img ? `<img src="${bidItem.img}" alt="${bidItem.name}">` : ""}</div>
    <div class="bid-card-info">
      <span class="tag available">Last One In Stock</span>
      <p class="card-brand${hasBrand(bidItem) ? "" : " card-brand-missing"}">${bidItem.brand}</p>
      <h3>${bidItem.name}</h3>
      <p class="card-meta">${bidItem.category} · ${bidItem.meta}</p>
      <p class="bid-card-desc">${bidItem.desc}</p>
      <div class="bid-current">
        <div>
          <span class="bid-current-label">Current Bid</span>
          <span class="bid-current-amount" id="bid-current-amount">$${bidItem.price}</span>
        </div>
        <span class="bid-current-name" id="bid-current-name">Starting price — no bids yet</span>
      </div>
      <form class="bid-form" id="bid-form">
        <input type="text" id="bid-name" placeholder="Your name" maxlength="40" required />
        <input type="number" id="bid-amount" placeholder="$${bidItem.price + 1}+" min="${bidItem.price + 1}" step="1" required />
        <button type="submit" class="btn btn-primary tilt" data-tilt-max="6">Place Bid</button>
      </form>
      <p class="bid-error" id="bid-error" hidden></p>
      <p class="bid-note">Ends Sunday at midnight. Highest bid wins — no snipe protection, so bid your max.</p>
    </div>
  `;

  document.getElementById("bid-form").addEventListener("submit", (e) => {
    e.preventDefault();
    submitBid();
  });
  document.querySelector(".bid-card-media").addEventListener("click", (e) => {
    if (e.target.tagName === "IMG") openLightbox(e.target.src, e.target.alt);
  });

  initTilt();
  refreshBidState();
}

function updateBidDisplay(current) {
  const amountEl = document.getElementById("bid-current-amount");
  if (!bidItem || !amountEl) return;
  const amountInput = document.getElementById("bid-amount");
  const nameEl = document.getElementById("bid-current-name");
  const minNext = (current && current.amount ? current.amount : bidItem.price) + 1;

  amountEl.textContent = current && current.amount ? `$${current.amount}` : `$${bidItem.price}`;
  nameEl.textContent = current && current.name ? `by ${current.name}` : "Starting price — no bids yet";
  amountInput.min = minNext;
  amountInput.placeholder = `$${minNext}+`;
}

async function refreshBidState() {
  if (!bidItem) return;
  // A backgrounded tab kept polling every 6s forever — a request, a JSON parse and a DOM update
  // for a page nobody is looking at, which on a phone is radio wake-ups and battery. Skipping
  // while hidden changes nothing on screen: `visibilitychange` below refreshes on the way back,
  // so the figure is already current by the time the page is visible again.
  if (document.hidden) return;
  try {
    const res = await fetch(`/api/bid?item=${encodeURIComponent(bidItem.name)}`);
    if (res.ok) {
      const current = await res.json();
      updateBidDisplay(current && current.amount ? current : null);
    }
  } catch (e) {
    // offline / static hosting — leave starting price shown
  }
}

async function submitBid() {
  const errorEl = document.getElementById("bid-error");
  const nameInput = document.getElementById("bid-name");
  const amountInput = document.getElementById("bid-amount");
  errorEl.hidden = true;

  const name = nameInput.value.trim();
  const amount = Number(amountInput.value);
  const minBid = Number(amountInput.min);

  if (!name || !amount || amount < minBid) {
    errorEl.textContent = `Enter a name and a bid of at least $${minBid}.`;
    errorEl.hidden = false;
    return;
  }

  try {
    const res = await fetch("/api/bid", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ item: bidItem.name, amount, name }),
    });
    const data = await res.json();
    if (data.ok) {
      updateBidDisplay(data.current);
      amountInput.value = "";
    } else {
      updateBidDisplay(data.current || null);
      errorEl.textContent = data.error || "Someone already bid higher — try a higher amount.";
      errorEl.hidden = false;
    }
  } catch (e) {
    errorEl.textContent = "Couldn't reach the server — try again in a moment.";
    errorEl.hidden = false;
  }
}

// ---------- featured picks (card stack) — ranked by visitor clicks ----------
// Used as the tiebreak/starting order until real click counts overtake it.
// Fallback order for the featured stack, read from the `featured` flags in products.json rather
// than a hardcoded list of names. Real visitor clicks still outrank it — this only decides the
// order before anyone has clicked anything, and breaks ties after.
function defaultFeaturedOrder() {
  return PRODUCTS.filter((p) => p.featured).map((p) => p.name);
}

let clickCounts = {};
let FEATURED = [];

async function loadClickCounts() {
  try {
    const res = await fetch("/api/stats");
    if (res.ok) clickCounts = await res.json();
  } catch (e) {
    // no backend available (e.g. static hosting) — fall back to default order
  }
}

// Recomputed on page load only, not mid-session, so cards don't shuffle under a browsing visitor.
function trackClick(name) {
  clickCounts[name] = (clickCounts[name] || 0) + 1;
  const body = JSON.stringify({ name });
  // sendBeacon hands the request to the browser to send on its own schedule, off the critical path,
  // so opening a product doesn't kick off a fetch that competes with rendering the modal. It also
  // survives the page being backgrounded mid-request. fetch stays as the fallback.
  if (navigator.sendBeacon) {
    try {
      navigator.sendBeacon("/api/click", new Blob([body], { type: "application/json" }));
      return;
    } catch (e) {
      // fall through to fetch
    }
  }
  fetch("/api/click", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
  }).catch(() => {});
}

// Flagged products come first, then the most-clicked fill whatever slots are left.
//
// This used to sort by clicks and consult `featured` only to break ties, which meant the flag did
// nothing the moment any product had a single click: marking a product Featured in the dashboard
// changed products.json correctly, and the storefront still showed the four most-clicked items.
// The flag is an instruction from the owner, so it now wins outright; clicks order the products
// within each group, and a catalog with nothing flagged still ranks purely by clicks as before.
function computeFeatured(n = 4) {
  const flagged = new Set(defaultFeaturedOrder());
  const byClicks = (a, b) => (clickCounts[b.name] || 0) - (clickCounts[a.name] || 0);
  const picked = PRODUCTS.filter((p) => flagged.has(p.name)).sort(byClicks);
  if (picked.length >= n) return picked.slice(0, n);
  const rest = PRODUCTS.filter((p) => !flagged.has(p.name)).sort(byClicks);
  return picked.concat(rest).slice(0, n);
}

function featuredStatsFor(p) {
  const sizes = sizesOf(p);
  return [
    ["Sizes", sizes.length > 1 ? `${sizes[0]}–${sizes[sizes.length - 1]}` : sizes[0]],
    ["Units", String(p.stock)],
    ["Price", `$${p.price}`],
    ["Status", p.status === "sold" ? "Sold Out" : "Available"],
  ];
}

const STACK_CARD_WIDTH = 320;
const STACK_CARD_OVERLAP = 240;
let stackExpanded = false;
let stackHoveredIndex = null;

function stackPose(index, total, expanded) {
  if (!expanded) {
    const centerOffset = (total - 1) * 5;
    return {
      x: index * 10 - centerOffset,
      y: index * 2,
      rot: index * 1.5,
    };
  }
  const totalExpandedWidth = STACK_CARD_WIDTH + (total - 1) * (STACK_CARD_WIDTH - STACK_CARD_OVERLAP);
  const expandedCenterOffset = totalExpandedWidth / 2;
  return {
    x: index * (STACK_CARD_WIDTH - STACK_CARD_OVERLAP) - expandedCenterOffset + STACK_CARD_WIDTH / 2,
    y: 0,
    rot: index * 5 - (total - 1) * 2.5,
  };
}

function renderFeatured() {
  const wrap = document.getElementById("stack-wrap");
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  FEATURED = computeFeatured();

  wrap.innerHTML = FEATURED.map((p, index) => `
    <div class="stack-card" style="z-index:${FEATURED.length - index}" data-index="${index}" tabindex="0">
      <dl class="stack-specs">
        ${featuredStatsFor(p).map(([label, value]) => `<div class="stack-spec"><dd class="stack-spec-value">${value}</dd><dt class="stack-spec-label">${label}</dt></div>`).join("")}
      </dl>
      <div class="stack-image">${p.img ? `<img src="${p.img}" alt="${p.name}" loading="lazy" decoding="async">` : `<span>Photo Coming</span>`}</div>
      <span class="stack-brand${hasBrand(p) ? "" : " card-brand-missing"}">${p.brand}</span>
      <span class="stack-title">${p.name}</span>
      <span class="stack-subtitle">${p.category}</span>
      <p class="stack-desc">${p.desc}</p>
    </div>
  `).join("");

  applyStackPoses(reduced);

  wrap.querySelectorAll(".stack-card").forEach((card) => {
    const index = Number(card.dataset.index);

    card.addEventListener("mouseenter", () => {
      stackHoveredIndex = index;
      applyStackPoses(reduced);
    });
    card.addEventListener("mouseleave", () => {
      stackHoveredIndex = null;
      applyStackPoses(reduced);
    });
    card.addEventListener("focus", () => {
      stackHoveredIndex = index;
      applyStackPoses(reduced);
    });
    card.addEventListener("blur", () => {
      stackHoveredIndex = null;
      applyStackPoses(reduced);
    });
  });
}

function applyStackPoses(reduced) {
  const wrap = document.getElementById("stack-wrap");
  wrap.querySelectorAll(".stack-card").forEach((card, index) => {
    const pose = stackPose(index, FEATURED.length, stackExpanded);
    const isHovered = stackExpanded && stackHoveredIndex === index;
    const isDimmed = stackExpanded && stackHoveredIndex !== null && stackHoveredIndex !== index;

    card.style.transitionDelay = stackExpanded && !reduced ? `${index * 40}ms` : "0ms";
    card.style.setProperty("--sx", `${pose.x}px`);
    card.style.setProperty("--sy", `${isHovered ? pose.y - 18 : pose.y}px`);
    card.style.setProperty("--srot", reduced ? "0deg" : `${pose.rot}deg`);
    card.style.setProperty("--sscale", isHovered ? "1.14" : isDimmed ? "0.94" : "1");
    card.style.setProperty("--sopacity", isDimmed ? "0.75" : "1");
    card.style.zIndex = isHovered ? String(FEATURED.length + 10) : String(FEATURED.length - index);
  });
}

// Spreads the stack on hover; whichever card is hovered scales up and becomes the focal point.
// Click still expands/opens on touch devices, which don't fire hover events.
function initStack() {
  const wrap = document.getElementById("stack-wrap");
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const expand = () => {
    if (stackExpanded) return;
    stackExpanded = true;
    wrap.setAttribute("aria-expanded", "true");
    applyStackPoses(reduced);
  };
  const collapse = () => {
    if (!stackExpanded) return;
    stackExpanded = false;
    stackHoveredIndex = null;
    wrap.setAttribute("aria-expanded", "false");
    applyStackPoses(reduced);
  };

  wrap.addEventListener("mouseenter", expand);
  wrap.addEventListener("mouseleave", collapse);
  wrap.addEventListener("focusin", expand);
  wrap.addEventListener("focusout", (e) => {
    if (!wrap.contains(e.relatedTarget)) collapse();
  });

  wrap.addEventListener("click", (e) => {
    if (!stackExpanded) {
      expand();
      return;
    }
    const card = e.target.closest(".stack-card");
    if (card) openModal(FEATURED[Number(card.dataset.index)]);
  });
  wrap.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      if (!stackExpanded) expand();
      else if (stackHoveredIndex !== null) openModal(FEATURED[stackHoveredIndex]);
    }
  });
}

function renderCategoryTiles() {
  const wrap = document.getElementById("category-tiles");

  wrap.innerHTML = CATEGORIES.map((c) => {
    const count = c === "All" ? PRODUCTS.length : PRODUCTS.filter((p) => p.category === c).length;
    const thumb = (PRODUCTS.find((p) => p.img && (c === "All" || p.category === c)) || {}).img;
    return `
      <button type="button" class="category-tile tilt ${c === state.category ? "active" : ""}" data-tilt-max="5" data-category="${c}">
        ${thumb ? `<img src="${thumb}" alt="" loading="lazy" decoding="async">` : ""}
        <div class="category-tile-overlay">
          <span class="category-tile-name">${c}</span>
          <span class="category-tile-count">${count} item${count === 1 ? "" : "s"}</span>
        </div>
      </button>
    `;
  }).join("");

  wrap.querySelectorAll(".category-tile").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.category = btn.dataset.category;
      document.getElementById("catalog-title").textContent =
        state.category === "All" ? "All Stock" : state.category;
      renderCategoryTiles();
      renderProducts();
      document.getElementById("catalog-head").scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });

  initTilt();
}

function renderSizeFilter() {
  const wrap = document.getElementById("size-filter");
  const sizes = [...new Set(PRODUCTS.flatMap(sizesOf))].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));

  wrap.innerHTML = sizes.map((s) => `
    <button type="button" class="size-chip ${state.sizes.includes(s) ? "active" : ""}" data-size="${s}">${s}</button>
  `).join("");

  wrap.querySelectorAll(".size-chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      const size = btn.dataset.size;
      state.sizes = state.sizes.includes(size)
        ? state.sizes.filter((s) => s !== size)
        : [...state.sizes, size];
      renderSizeFilter();
      renderProducts();
    });
  });
}

function getFilteredProducts() {
  let items = PRODUCTS.filter((p) => {
    const matchesCategory = state.category === "All" || p.category === state.category;
    const q = state.search.toLowerCase();
    const matchesSearch = fullName(p).toLowerCase().includes(q);
    const matchesStock = !state.inStockOnly || p.status === "available";
    const matchesSize = state.sizes.length === 0 || sizesOf(p).some((sz) => state.sizes.includes(sz));
    const matchesMin = state.minPrice == null || p.price >= state.minPrice;
    const matchesMax = state.maxPrice == null || p.price <= state.maxPrice;
    return matchesCategory && matchesSearch && matchesStock && matchesSize && matchesMin && matchesMax;
  });

  if (state.sort === "price-asc") items = items.slice().sort((a, b) => a.price - b.price);
  if (state.sort === "price-desc") items = items.slice().sort((a, b) => b.price - a.price);

  return items;
}

// One delegated click handler for the whole grid, bound once, instead of one listener per card
// re-attached on every render. Cards are matched by the `data-index` they already carry.
let gridClickBound = false;

function bindGridClicks(grid) {
  if (gridClickBound) return;
  gridClickBound = true;
  grid.addEventListener("click", (e) => {
    const card = e.target.closest(".card");
    if (!card || !grid.contains(card)) return;
    const index = Number(card.dataset.index);
    if (!Number.isNaN(index)) openModal(PRODUCTS[index]);
  });
}

// Signature of what the grid is currently showing. Typing in the search box fires an input event
// per keystroke, and most keystrokes don't change which products match — rebuilding 20 cards of
// innerHTML (throwing away decoded images and forcing a full relayout) to produce byte-identical
// markup is pure waste on a phone. Same output, so nothing visual depends on this.
let lastGridSignature = null;

function renderProducts() {
  const grid = document.getElementById("product-grid");
  const emptyState = document.getElementById("empty-state");
  const items = getFilteredProducts();

  bindGridClicks(grid);

  emptyState.hidden = items.length > 0;
  grid.hidden = items.length === 0;

  const signature = items.map((p) => `${PRODUCTS.indexOf(p)}:${p.price}:${p.stock}:${p.status}`).join("|");
  if (signature === lastGridSignature) return;
  lastGridSignature = signature;

  grid.innerHTML = items.map((p, i) => `
    <div class="card tilt" data-tilt-max="6" data-index="${PRODUCTS.indexOf(p)}">
      <div class="card-media">
        ${p.img ? `<img src="${p.img}" alt="${p.name}" loading="lazy" decoding="async">` : `<span>Photo Coming</span>`}
        <div class="tilt-glow"></div>
        <span class="tag ${p.status}">${p.status === "sold" ? "Sold Out" : "Available"}</span>
      </div>
      <div class="card-body">
        <p class="card-brand${hasBrand(p) ? "" : " card-brand-missing"}">${p.brand}</p>
        <p class="card-title">${p.name}</p>
        <p class="card-meta">${p.meta}</p>
        <div class="card-foot">
          <div class="price-block">
            <span class="price">$${p.price}</span>
            ${p.status === "sold" ? "" : `<span class="stock-note">${stockLabel(p)}</span>`}
          </div>
          <button type="button" class="card-cta" data-index="${PRODUCTS.indexOf(p)}">
            <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><circle cx="12" cy="8" r="0.5" fill="currentColor"/></svg>
            Details
          </button>
        </div>${bulkStripHtml(p)}
      </div>
    </div>
  `).join("");

  initTilt();
}

// ---------- item detail modal ----------
function openModal(p) {
  trackClick(p.name);
  currentModalProduct = p;
  document.getElementById("modal-media").innerHTML = p.img
    ? `<img src="${p.img}" alt="${p.name}">`
    : `<span id="modal-media-label">Photo Coming</span>`;
  const tag = document.getElementById("modal-tag");
  tag.textContent = p.status === "sold" ? "Sold Out" : "Available";
  tag.className = `tag ${p.status}`;
  const brandEl = document.getElementById("modal-brand");
  brandEl.textContent = p.brand;
  brandEl.classList.toggle("card-brand-missing", !hasBrand(p));
  document.getElementById("modal-title").textContent = p.name;
  document.getElementById("modal-meta").textContent = p.category;
  // Auto-pick when there's only one option, so one-size pieces need no extra tap.
  selectedSize = p.sizes.length === 1 ? p.sizes[0].size : null;
  selectedQty = 1;
  renderModalSizes();
  renderModalQty();
  document.getElementById("modal-desc").textContent = p.desc;
  document.getElementById("modal-price").textContent = `$${p.price}`;
  document.getElementById("modal-stock-note").textContent = p.status === "sold" ? "" : stockLabel(p);
  document.getElementById("modal-bulk").innerHTML = bulkTableHtml(p);
  syncModalActions();
  document.getElementById("modal-overlay").hidden = false;
  syncBodyScroll();
}

// Chips double as the availability breakdown and the picker — each shows its own units left.
function renderModalSizes() {
  const p = currentModalProduct;
  document.getElementById("modal-sizes").innerHTML = p.sizes
    .map((s) => `
      <button type="button" class="size-pill${s.size === selectedSize ? " selected" : ""}"
              data-size="${s.size}" aria-pressed="${s.size === selectedSize}">
        <b>${s.size}</b>${s.qty > 1 ? ` &times;${s.qty}` : ""}
      </button>
    `).join("");

  document.getElementById("modal-sizes").querySelectorAll(".size-pill").forEach((btn) => {
    btn.addEventListener("click", () => {
      selectedSize = btn.dataset.size;
      selectedQty = 1;
      renderModalSizes();
      renderModalQty();
      syncModalActions();
    });
  });
}

// The stepper only appears once a size is chosen, since stock is per-size.
function renderModalQty() {
  const row = document.getElementById("modal-qty-row");
  row.hidden = !selectedSize;
  if (!selectedSize) return;

  const max = unitsFor(currentModalProduct, selectedSize);
  document.getElementById("qty-value").textContent = String(selectedQty);
  document.getElementById("qty-minus").disabled = selectedQty <= 1;
  document.getElementById("qty-plus").disabled = selectedQty >= max;
  document.getElementById("qty-max").textContent = `${max} available in ${selectedSize}`;
}

// Buying needs a size, so both actions stay disabled until one is picked.
function syncModalActions() {
  const sold = currentModalProduct.status === "sold";
  const needsSize = !sold && !selectedSize;
  const buyBtn = document.getElementById("buy-now-btn");
  const cartBtn = document.getElementById("add-to-cart-btn");

  buyBtn.disabled = sold || needsSize;
  buyBtn.textContent = sold ? "Sold Out" : needsSize ? "Select a Size" : "Buy Now";
  cartBtn.disabled = sold || needsSize;
  cartBtn.textContent = "Add to Cart";
}

function closeModal() {
  document.getElementById("modal-overlay").hidden = true;
  syncBodyScroll();
}

function initModal() {
  document.getElementById("modal-close").addEventListener("click", closeModal);
  document.getElementById("modal-overlay").addEventListener("click", (e) => {
    if (e.target.id === "modal-overlay") closeModal();
  });
  document.getElementById("modal-media").addEventListener("click", (e) => {
    if (e.target.tagName === "IMG") openLightbox(e.target.src, e.target.alt);
  });
  document.getElementById("qty-minus").addEventListener("click", () => {
    selectedQty = clampQty(currentModalProduct, selectedSize, selectedQty - 1);
    renderModalQty();
  });
  document.getElementById("qty-plus").addEventListener("click", () => {
    selectedQty = clampQty(currentModalProduct, selectedSize, selectedQty + 1);
    renderModalQty();
  });
  document.getElementById("buy-now-btn").addEventListener("click", () => {
    if (!currentModalProduct || currentModalProduct.status === "sold" || !selectedSize) return;
    const line = cartLine(currentModalProduct, selectedSize, selectedQty);
    closeModal();
    openCheckout([line]);
  });
  document.getElementById("add-to-cart-btn").addEventListener("click", (e) => {
    if (!currentModalProduct || currentModalProduct.status === "sold" || !selectedSize) return;
    const result = addToCart(currentModalProduct, selectedSize, selectedQty);
    const btn = e.currentTarget;
    btn.textContent = { added: "Added ✓", "topped-up": "Cart Updated ✓", maxed: "All Stock In Cart",
                        unavailable: "Unavailable" }[result] || "Unavailable";
    setTimeout(() => { btn.textContent = "Add to Cart"; }, 1500);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeLightbox();
      closeModal();
      closeCheckout();
      closeCart();
      closeOrders();
    }
  });
}

// ---------- image lightbox (click product photo to zoom) ----------
function openLightbox(src, alt) {
  document.getElementById("lightbox-img").src = src;
  document.getElementById("lightbox-img").alt = alt;
  document.getElementById("lightbox-overlay").hidden = false;
}

function closeLightbox() {
  document.getElementById("lightbox-overlay").hidden = true;
}

function initLightbox() {
  document.getElementById("lightbox-close").addEventListener("click", closeLightbox);
  document.getElementById("lightbox-overlay").addEventListener("click", (e) => {
    if (e.target.id === "lightbox-overlay") closeLightbox();
  });
}

function syncBodyScroll() {
  const anyOpen = ["modal-overlay", "checkout-overlay", "cart-overlay", "orders-overlay"]
    .some((id) => !document.getElementById(id).hidden);
  document.body.style.overflow = anyOpen ? "hidden" : "";
}

// `removable` rows (the cart) get a live quantity stepper; read-only rows (checkout) show "x N".
function checkoutItemRow(line, removable, lines) {
  const sizeLabel = line.size === "One Size" ? "One Size" : `Size ${line.size}`;
  const unitPrice = linePrice(line, lines);
  // Retail is the reference the discount is legible against. Without it the line just shows a
  // number that quietly changed when some other style was added to the basket.
  const retailPrice = priceFor(line, 1);
  const discounted = retailPrice - unitPrice > 0.005;
  const tierLabel = discounted ? (tierFor(line, poolUnitsIn(lines, line)) || {}).label : null;
  const max = unitsFor(line, line.size);
  const qtyControl = removable
    ? `<div class="qty-stepper qty-stepper-sm">
         <button type="button" data-line-id="${line.lineId}" data-step="-1" ${line.qty <= 1 ? "disabled" : ""} aria-label="Decrease quantity">&minus;</button>
         <span>${line.qty}</span>
         <button type="button" data-line-id="${line.lineId}" data-step="1" ${line.qty >= max ? "disabled" : ""} aria-label="Increase quantity">+</button>
       </div>`
    : `<span class="checkout-item-qty">&times;${line.qty}</span>`;

  return `
    <div class="checkout-item">
      <div class="checkout-item-media">${line.img ? `<img src="${line.img}" alt="${line.name}">` : ""}</div>
      <div class="checkout-item-info">
        <span class="checkout-item-name">${fullName(line)}</span>
        <span class="checkout-item-meta">${line.category} &middot; ${sizeLabel}</span>
        ${discounted ? `<span class="checkout-item-tier">${tierLabel || "Bulk"} &middot; ${money(unitPrice)} ea</span>` : ""}
      </div>
      ${qtyControl}
      <span class="price">${discounted ? `<s class="price-was">${money(retailPrice * line.qty)}</s>` : ""}${money(unitPrice * line.qty)}</span>
      ${removable ? `<button type="button" class="checkout-item-remove" data-line-id="${line.lineId}" aria-label="Remove">&times;</button>` : ""}
    </div>
  `;
}

// Every money figure runs through here so price x quantity is never summed by hand. The per-unit
// price depends on how many of that style the basket holds, so the whole list is passed to each line.
function lineTotal(lines) {
  return lines.reduce((sum, l) => sum + linePrice(l, lines) * l.qty, 0);
}

// ---------- checkout (reserves stock; no payment processor connected) ----------
// The card-number, expiry and CVC formatters lived here. They are gone along with the inputs:
// formatting a card number implies the site does something with it, and it did not.
// items is always an array — a lone "Buy Now" is a one-item array, cart checkout is the whole cart.
function openCheckout(items) {
  checkoutItems = items;
  checkoutKey = "co_" + Date.now().toString(36) + "_" + Math.random().toString(36).slice(2, 10);
  const err = document.getElementById("checkout-error");
  if (err) err.hidden = true;
  const subtotal = lineTotal(items);
  const shipping = shippingFor(items);
  const total = subtotal + shipping;

  document.getElementById("checkout-items").innerHTML = items.map((p) => checkoutItemRow(p, false, items)).join("");
  document.getElementById("checkout-bulk-feedback").innerHTML = bulkFeedbackHtml(items, false);
  document.getElementById("checkout-subtotal").textContent = money(subtotal);

  document.getElementById("checkout-form").reset();
  document.getElementById("checkout-form-view").hidden = false;
  document.getElementById("checkout-success-view").hidden = true;

  const payBtn = document.getElementById("checkout-pay-btn");
  payBtn.disabled = false;
  applyPaymentCopy(total);
  // Opens on the flat-ladder estimate, then replaces it the moment a ZIP resolves to a zone.
  setCheckoutShipping(shipping, "estimate", null);
  refreshCheckoutShipping();

  document.getElementById("checkout-overlay").hidden = false;
  syncBodyScroll();
}

function closeCheckout() {
  document.getElementById("checkout-overlay").hidden = true;
  syncBodyScroll();
}

// Shared success step for both the card form and the Apple Pay button —
// both are front-end previews only, nothing is actually charged. Still records
// a real order (server-side) so My Orders has something to look up.
function completeCheckout(triggerLabelEl, method, successMessage, email) {
  triggerLabelEl.textContent = "Processing...";
  const items = checkoutItems;
  const subtotal = lineTotal(items);
  const shipping = shippingFor(items);
  const val = (id) => document.getElementById(id).value.trim();

  // Kept as separate fields because Pirate Ship's spreadsheet upload needs one column each.
  const ship_to = {
    name: val("co-ship-name"),
    address1: val("co-address1"),
    address2: val("co-address2"),
    city: val("co-city"),
    state: val("co-state").toUpperCase(),
    zip: val("co-zip"),
    country: "US",
  };

  // The server prices the order itself and can refuse it — stock may have gone since the cart was
  // filled. So this WAITS for the answer. It used to fire and forget, empty the cart and show
  // "Payment successful" regardless, which would now turn a genuine rejection into a fake sale.
  //
  // `price` and `tier` are sent for the record only; the server recomputes both and ignores what
  // arrives here. `id` is what actually identifies the product.
  fetch("/api/order", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      email,
      subtotal,
      shipping,
      total: subtotal + shipping,
      weight_oz: orderWeightOz(items),
      ship_to,
      idempotency_key: checkoutKey,
      items: items.map((l) => ({
        id: l.id,
        name: fullName(l),
        size: l.size,
        qty: l.qty,
        price: linePrice(l, items),
        tier: (tierFor(l, poolUnitsIn(items, l)) || {}).id || "retail",
      })),
    }),
  })
    .then((res) => res.json().then((out) => ({ ok: res.ok && out.ok, out })))
    .then(({ ok, out }) => {
      if (!ok) {
        // Cart deliberately left intact so the buyer can adjust and try again.
        triggerLabelEl.textContent = "Try again";
        showCheckoutError(out.error || "That order could not be placed.");
        return;
      }
      items.forEach((l) => removeFromCart(l.lineId));
      document.getElementById("checkout-form-view").hidden = true;
      document.getElementById("checkout-success-view").hidden = false;
      // Not "Payment successful": nothing has been paid. Saying otherwise is the same dishonesty
      // as collecting card numbers that go nowhere.
      document.getElementById("payment-alert-title").textContent = "Order reserved — no payment taken";
      document.getElementById("payment-alert-desc").textContent =
        successMessage + (out.ref ? ` Your order reference is ${out.ref}.` : "");
    })
    .catch(() => {
      triggerLabelEl.textContent = "Try again";
      showCheckoutError("Could not reach the server. Nothing has been charged.");
    });
}

// ---------- zone shipping ----------
// The cart shows an ESTIMATE: it has no address, so it prices off the flat ladder. The checkout
// panel asks the SERVER for the real figure as soon as a ZIP is entered — never recomputed here,
// so the number on screen is the number that will be charged. A second implementation in the
// browser is exactly how a cart figure and a card charge end up disagreeing.
let shippingQuoteSeq = 0;
let checkoutShippingSource = "estimate";

// One writer for the shipping figure, the total and the wording, so they cannot disagree on screen.
function setCheckoutShipping(shipping, source, group) {
  const subtotal = lineTotal(checkoutItems);
  const total = subtotal + shipping;
  document.getElementById("checkout-shipping").textContent = money(shipping);
  document.getElementById("checkout-total-price").textContent = money(total);

  const lb = (orderWeightOz(checkoutItems) / 16).toFixed(1);
  const note = document.getElementById("checkout-ship-note");
  if (note) {
    note.textContent = source === "zone"
      ? `(${lb} lb, Ground Advantage, ${group || "your"} zone)`
      : `(${lb} lb, estimated — enter your ZIP for the exact rate)`;
  }
  const payAmount = document.getElementById("checkout-pay-amount");
  if (payAmount) payAmount.textContent = money(total);
  checkoutShippingSource = source;
}

async function refreshCheckoutShipping() {
  const zipEl = document.getElementById("co-zip");
  const zip = (zipEl ? zipEl.value : "").trim();
  if (!checkoutItems.length) return;

  // A US ZIP is five digits; anything shorter cannot resolve to a zone, so don't ask.
  if (zip.replace(/\D/g, "").length < 5) {
    setCheckoutShipping(shippingFor(checkoutItems), "estimate", null);
    return;
  }

  const seq = ++shippingQuoteSeq;
  try {
    const res = await fetch("/api/shipping/quote", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        items: checkoutItems.map((l) => ({ id: l.id, name: fullName(l), size: l.size, qty: l.qty })),
        zip,
      }),
    });
    const data = await res.json();
    if (seq !== shippingQuoteSeq) return;   // a slow earlier reply must not overwrite a newer one
    if (data && data.ok) {
      setCheckoutShipping(data.shipping, data.source, data.zone_group);
      return;
    }
  } catch (err) { /* offline or file:// — fall through to the estimate */ }
  if (seq === shippingQuoteSeq) setCheckoutShipping(shippingFor(checkoutItems), "estimate", null);
}

// ---------- stripe checkout ----------
// Hosted Checkout: the buyer leaves for Stripe's page and comes back. Card details never touch
// this site, which is why no card fields were ever added back to the form.
//
// Payments are OFF unless the server says otherwise, and the server only says so when it has both
// an API key and a webhook secret. So a fresh clone, a `file://` page, or a half-configured
// install all fall back to the reserve flow rather than offering a button that cannot work.
const PAYMENTS = { enabled: false, mode: "off" };

// Which cart lines a redirect is paying for, so coming back successful clears exactly those and
// not a piece added in another tab while Stripe had the buyer.
const PENDING_CHECKOUT_KEY = "poc_pending_checkout";

async function loadPaymentsConfig() {
  try {
    const res = await fetch("/api/payments/config");
    const data = await res.json();
    PAYMENTS.enabled = !!data.payments_enabled;
    PAYMENTS.mode = data.mode || "off";
  } catch (err) {
    // file://, or the server is down. Either way: no card payment, no error shown to the visitor.
    PAYMENTS.enabled = false;
    PAYMENTS.mode = "off";
  }
}

// The reserve flow and the card flow make different promises, so the panel's copy has to move with
// them. Leaving "no card details collected" up while Stripe collects card details would be exactly
// the kind of claim that got "Encrypted checkout" removed.
function applyPaymentCopy(total) {
  const notice = document.getElementById("checkout-notice");
  const trustCard = document.getElementById("checkout-trust-card");
  const payLabel = document.getElementById("checkout-pay-label");
  const eyebrow = document.getElementById("checkout-eyebrow");
  const disclaimer = document.getElementById("checkout-disclaimer");

  if (!PAYMENTS.enabled) {
    payLabel.innerHTML = `Reserve <span id="checkout-pay-amount">${money(total)}</span>`;
    return;
  }

  payLabel.innerHTML = `Pay <span id="checkout-pay-amount">${money(total)}</span>`;
  if (trustCard) trustCard.textContent = "Card details go straight to Stripe";
  if (eyebrow) eyebrow.textContent = "Card Payment";
  // The reserve flow's fine print says no payment is taken — true then, false the moment Stripe is
  // taking one. Every line that makes a promise has to move with the flow, not just the button.
  if (disclaimer) {
    disclaimer.textContent =
      PAYMENTS.mode === "test"
        ? "Stripe test mode: no card is charged and no money moves. Your items are held for 30 minutes."
        : "Payment is processed by Stripe. Your items are held for 30 minutes while you pay.";
  }
  if (notice) {
    // In test mode this says so in the loudest place on the panel. A test charge that looks like a
    // real one is how someone ends up believing they have been paid.
    notice.innerHTML =
      PAYMENTS.mode === "test"
        ? "<strong>Test mode — no real money moves.</strong> This checkout is wired to Stripe's test " +
          "environment. Use card 4242 4242 4242 4242, any future expiry and any CVC. A real card will be declined."
        : "<strong>Payment is handled by Stripe.</strong> You'll be sent to Stripe's secure checkout " +
          "page to pay, then brought back here. Your card details never touch this site.";
  }
}

// Server-side pricing is the whole point: this posts name/size/qty and nothing else. The order is
// priced, stock-checked and reserved by the server, and the Stripe session is built from those
// figures — so what the card is charged is what the shop computed, not what the page displayed.
function startStripeCheckout(labelEl, email, ship_to) {
  labelEl.textContent = "Taking you to Stripe...";
  const items = checkoutItems;

  fetch("/api/checkout/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      email,
      ship_to,
      idempotency_key: checkoutKey,
      items: items.map((l) => ({ id: l.id, name: fullName(l), size: l.size, qty: l.qty })),
    }),
  })
    .then((res) => res.json().then((out) => ({ ok: res.ok && out.ok, out })))
    .then(({ ok, out }) => {
      if (!ok || !out.url) {
        labelEl.textContent = "Try again";
        document.getElementById("checkout-pay-btn").disabled = false;
        showCheckoutError(out.error || "Could not start the payment. Nothing has been charged.");
        return;
      }
      // Recorded before leaving: the browser may not come back for minutes, or at all.
      try {
        localStorage.setItem(PENDING_CHECKOUT_KEY, JSON.stringify({
          ref: out.ref, lineIds: items.map((l) => l.lineId),
        }));
      } catch (err) { /* private mode — the cart just won't self-clear */ }
      window.location.href = out.url;
    })
    .catch(() => {
      labelEl.textContent = "Try again";
      document.getElementById("checkout-pay-btn").disabled = false;
      showCheckoutError("Could not reach the server. Nothing has been charged.");
    });
}

// Coming back from Stripe. The query string is NOT evidence of anything — anyone can type
// ?checkout=success — so the order's real status is read from the server before a word is shown.
async function initCheckoutReturn() {
  const params = new URLSearchParams(window.location.search);
  const outcome = params.get("checkout");
  if (!outcome) return;

  const ref = (params.get("ref") || "").trim();
  // Clean the URL either way, so a refresh doesn't replay this and the ref doesn't linger in the
  // address bar to be copied into a screenshot.
  window.history.replaceState({}, "", window.location.pathname);

  let pending = null;
  try {
    pending = JSON.parse(localStorage.getItem(PENDING_CHECKOUT_KEY) || "null");
  } catch (err) { /* ignore */ }
  try {
    localStorage.removeItem(PENDING_CHECKOUT_KEY);
  } catch (err) { /* ignore */ }

  // Reuses the panel's existing alert view rather than inventing a toast: same two elements the
  // reserve flow writes into, same Done button.
  const showAlert = (titleText, descText) => {
    document.getElementById("payment-alert-title").textContent = titleText;
    document.getElementById("payment-alert-desc").textContent = descText;
    document.getElementById("checkout-form-view").hidden = true;
    document.getElementById("checkout-success-view").hidden = false;
    document.getElementById("checkout-overlay").hidden = false;
    syncBodyScroll();
  };

  if (outcome === "cancelled") {
    // Stock is released by the webhook on checkout.session.expired, or by the reservation TTL.
    // The cart is deliberately left exactly as it was so the buyer can simply try again.
    showAlert("Payment cancelled", "Nothing was charged and your cart is still here.");
    return;
  }
  if (outcome !== "success" || !ref) return;

  let state = null;
  try {
    const res = await fetch(`/api/checkout/status?ref=${encodeURIComponent(ref)}`);
    state = await res.json();
  } catch (err) { /* handled below */ }

  if (state && state.ok && state.paid) {
    if (pending && Array.isArray(pending.lineIds)) pending.lineIds.forEach((id) => removeFromCart(id));
    showAlert(
      PAYMENTS.mode === "test" ? "Test payment complete" : "Payment received",
      `Order ${ref} is paid${PAYMENTS.mode === "test" ? " (test mode — no real money moved)" : ""}. ` +
        "A confirmation follows once shipping is arranged."
    );
  } else {
    // Stripe redirected but our webhook has not landed yet. Saying "paid" here would be guessing,
    // and the cart is left alone until the server agrees the money arrived.
    showAlert(
      "Payment is confirming",
      `Order ${ref} was submitted and is waiting on confirmation from Stripe. ` +
        "Check My Orders in a moment — nothing further is needed from you."
    );
  }
}

// One key per checkout attempt, so a double-click or a retried request cannot create two orders.
let checkoutKey = null;

function showCheckoutError(message) {
  let box = document.getElementById("checkout-error");
  if (!box) {
    box = document.createElement("div");
    box.id = "checkout-error";
    box.className = "checkout-error";
    const view = document.getElementById("checkout-form-view");
    view.insertBefore(box, view.firstChild);
  }
  box.textContent = message;
  box.hidden = false;
  box.scrollIntoView({ behavior: "smooth", block: "center" });
}

function initCheckout() {
  document.getElementById("checkout-close").addEventListener("click", closeCheckout);
  document.getElementById("checkout-done-btn").addEventListener("click", closeCheckout);
  document.getElementById("checkout-overlay").addEventListener("click", (e) => {
    if (e.target.id === "checkout-overlay") closeCheckout();
  });

  const zipEl = document.getElementById("co-zip");
  if (zipEl) {
    let t = null;
    zipEl.addEventListener("input", () => {
      clearTimeout(t);
      t = setTimeout(refreshCheckoutShipping, 250);   // one request per pause, not per keystroke
    });
    zipEl.addEventListener("blur", refreshCheckoutShipping);
  }

  document.getElementById("checkout-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const payBtn = document.getElementById("checkout-pay-btn");
    const email = document.getElementById("co-email").value;
    payBtn.disabled = true;
    const label = checkoutItems.length > 1 ? `Your ${checkoutItems.length} items are` : `${checkoutItems[0].name} is`;
    const val = (id) => document.getElementById(id).value.trim();

    if (PAYMENTS.enabled) {
      startStripeCheckout(document.getElementById("checkout-pay-label"), email, {
        name: val("co-ship-name"),
        address1: val("co-address1"),
        address2: val("co-address2"),
        city: val("co-city"),
        state: val("co-state").toUpperCase(),
        zip: val("co-zip"),
        country: "US",
      });
      return;
    }

    completeCheckout(
      document.getElementById("checkout-pay-label"),
      "Reservation",   // `method` is now only used for the order record, not the headline
      `${label} on hold. A confirmation will go to ${email} once shipping is arranged.`,
      email
    );
  });

}

// ---------- cart ----------
// A cart entry is a *line*: the product plus the size the buyer picked. Two sizes of one style are
// two lines. Spreading the product keeps every existing `line.price` / `line.img` read working.
let cart = [];

function cartLine(p, size, qty = 1) {
  return { ...p, size, qty: clampQty(p, size, qty), lineId: `${p.name}__${size}` };
}

// Units on hand for one size of a style — the ceiling on everything quantity-related.
function unitsFor(p, size) {
  const entry = p.sizes.find((s) => s.size === size);
  return entry ? entry.qty : 0;
}

function clampQty(p, size, qty) {
  return Math.max(1, Math.min(Math.round(qty) || 1, unitsFor(p, size)));
}

function loadCart() {
  try {
    const saved = JSON.parse(localStorage.getItem("poc_cart") || "[]");
    cart = saved
      .map(({ name, size, qty }) => {
        const p = PRODUCTS.find((x) => x.name === name);
        // Drop lines whose style or size has gone. `status === "sold"` matters as much as a
        // missing size: a cart saved before a sell-out would otherwise survive in localStorage
        // and carry an unavailable item all the way to checkout.
        if (!p || p.status === "sold" || !sizesOf(p).includes(size)) return null;
        if (unitsFor(p, size) < 1) return null;
        return cartLine(p, size, qty);
      })
      .filter(Boolean);
  } catch (e) {
    cart = [];
  }
}

function saveCart() {
  localStorage.setItem("poc_cart", JSON.stringify(cart.map((l) => ({ name: l.name, size: l.size, qty: l.qty }))));
}

function updateCartBadge() {
  const badge = document.getElementById("cart-badge");
  const units = cart.reduce((n, l) => n + l.qty, 0);
  badge.textContent = String(units);
  badge.hidden = units === 0;
}

// Returns "added", "topped-up", "maxed", or "unavailable" so the button can say what happened.
function addToCart(p, size, qty = 1) {
  // The buttons that call this are disabled for a sold-out style, but the guard belongs here too:
  // this is the money path, and a stale page, a restored tab or any future caller would otherwise
  // walk a sold item straight into the cart.
  if (!p || p.status === "sold" || unitsFor(p, size) < 1) return "unavailable";
  const line = cartLine(p, size, qty);
  const existing = cart.find((l) => l.lineId === line.lineId);
  if (existing) {
    const before = existing.qty;
    existing.qty = clampQty(p, size, existing.qty + line.qty);
    saveCart();
    updateCartBadge();
    return existing.qty === before ? "maxed" : "topped-up";
  }
  cart.push(line);
  saveCart();
  updateCartBadge();
  return "added";
}

function setLineQty(lineId, qty) {
  const line = cart.find((l) => l.lineId === lineId);
  if (!line) return;
  line.qty = clampQty(line, line.size, qty);
  saveCart();
  updateCartBadge();
  if (!document.getElementById("cart-overlay").hidden) renderCartItems();
}

function removeFromCart(lineId) {
  if (!cart.some((l) => l.lineId === lineId)) return;
  cart = cart.filter((l) => l.lineId !== lineId);
  saveCart();
  updateCartBadge();
  if (!document.getElementById("cart-overlay").hidden) renderCartItems();
}

function renderCartItems() {
  const wrap = document.getElementById("cart-items");
  const emptyNote = document.getElementById("cart-empty-note");
  const totalRow = document.getElementById("cart-total-row");
  const checkoutBtn = document.getElementById("cart-checkout-btn");

  emptyNote.hidden = cart.length > 0;
  totalRow.hidden = cart.length === 0;
  document.getElementById("cart-subtotal-row").hidden = cart.length === 0;
  document.getElementById("cart-shipping-row").hidden = cart.length === 0;
  checkoutBtn.hidden = cart.length === 0;

  wrap.innerHTML = cart.map((p) => checkoutItemRow(p, true, cart)).join("");
  document.getElementById("cart-bulk-feedback").innerHTML = bulkFeedbackHtml(cart, true);
  const cartSub = lineTotal(cart);
  const cartShip = shippingFor(cart);
  document.getElementById("cart-subtotal").textContent = money(cartSub);
  document.getElementById("cart-shipping").textContent = money(cartShip);
  document.getElementById("cart-ship-note").textContent = cart.length ? `(${(orderWeightOz(cart) / 16).toFixed(1)} lb, before ZIP)` : "";
  document.getElementById("cart-total-price").textContent = money(cartSub + cartShip);

  bindCartClicks(wrap);
}

// Delegated once instead of re-bound on every render. This list re-renders on every single tap of
// a quantity stepper, and each render was attaching a fresh listener to every remove button and
// every stepper button in the cart — work that grows with cart size and repeats on each tap.
let cartClickBound = false;

function bindCartClicks(wrap) {
  if (cartClickBound) return;
  cartClickBound = true;
  wrap.addEventListener("click", (e) => {
    const remove = e.target.closest(".checkout-item-remove");
    if (remove) {
      removeFromCart(remove.dataset.lineId);
      return;
    }
    const step = e.target.closest(".qty-stepper button");
    if (step) {
      const line = cart.find((l) => l.lineId === step.dataset.lineId);
      if (line) setLineQty(line.lineId, line.qty + Number(step.dataset.step));
    }
  });
}

function openCart() {
  renderCartItems();
  document.getElementById("cart-overlay").hidden = false;
  syncBodyScroll();
}

function closeCart() {
  document.getElementById("cart-overlay").hidden = true;
  syncBodyScroll();
}

function initCart() {
  document.getElementById("cart-btn").addEventListener("click", openCart);
  document.getElementById("cart-close").addEventListener("click", closeCart);
  document.getElementById("cart-overlay").addEventListener("click", (e) => {
    if (e.target.id === "cart-overlay") closeCart();
  });
  document.getElementById("cart-checkout-btn").addEventListener("click", () => {
    if (cart.length === 0) return;
    closeCart();
    openCheckout(cart.slice());
  });
}

// ---------- my orders (email lookup — no accounts) ----------
function orderRow(order) {
  const date = new Date(order.time * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  const itemsSummary = order.items
    .map((it) => `${it.qty || 1}x ${it.name}${it.size ? ` (${it.size})` : ""}`)
    .join(", ");
  const s = order.ship_to;
  const addr = s
    ? [s.name, s.address1, s.address2, `${s.city}, ${s.state} ${s.zip}`].filter(Boolean).join(", ")
    : order.address || "Not provided"; // orders placed before the address split
  return `
    <div class="order-card">
      <div class="order-card-head">
        <h4>Order #${order.id}</h4>
        <button type="button" class="order-toggle-btn" aria-label="Toggle details" aria-expanded="false">
          <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m7 15 5 5 5-5"/><path d="m7 9 5-5 5 5"/></svg>
        </button>
      </div>
      <div class="order-field-row">
        <span class="order-field-label">Status</span>
        <span class="order-field-value">${order.status}</span>
      </div>
      <div class="order-details" hidden>
        <div class="order-field-row order-field-block">
          <p class="order-field-label">Placed</p>
          <p class="order-field-value">${date} &middot; ${money(order.total)}${
            order.shipping != null ? ` (incl. ${money(order.shipping)} shipping)` : ""
          }</p>
        </div>
        <div class="order-field-row order-field-block">
          <p class="order-field-label">Shipping address</p>
          <p class="order-field-value">${addr}</p>
        </div>
        <div class="order-field-row order-field-block">
          <p class="order-field-label">Items</p>
          <p class="order-field-value">${itemsSummary}</p>
        </div>
      </div>
    </div>
  `;
}

function initOrders() {
  document.getElementById("orders-btn").addEventListener("click", () => {
    document.getElementById("orders-overlay").hidden = false;
    syncBodyScroll();
  });
  document.getElementById("orders-close").addEventListener("click", closeOrders);
  document.getElementById("orders-overlay").addEventListener("click", (e) => {
    if (e.target.id === "orders-overlay") closeOrders();
  });

  document.getElementById("orders-list").addEventListener("click", (e) => {
    const btn = e.target.closest(".order-toggle-btn");
    if (!btn) return;
    const details = btn.closest(".order-card").querySelector(".order-details");
    const open = details.hidden;
    details.hidden = !open;
    btn.setAttribute("aria-expanded", String(open));
    btn.classList.toggle("open", open);
  });

  document.getElementById("orders-lookup-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const email = document.getElementById("orders-email").value.trim();
    const ref = document.getElementById("orders-ref").value.trim();
    const list = document.getElementById("orders-list");
    const emptyNote = document.getElementById("orders-empty-note");
    list.innerHTML = "";
    emptyNote.textContent = "No matching order found. Check your email and order reference.";
    emptyNote.hidden = true;

    try {
      const res = await fetch(`/api/orders?email=${encodeURIComponent(email)}&ref=${encodeURIComponent(ref)}`);
      const orders = res.ok ? await res.json() : [];
      if (orders.length === 0) {
        emptyNote.hidden = false;
      } else {
        list.innerHTML = orders.slice().reverse().map(orderRow).join("");
      }
    } catch (err) {
      emptyNote.textContent = "Couldn't reach the server — try again in a moment.";
      emptyNote.hidden = false;
    }
  });
}

function closeOrders() {
  document.getElementById("orders-overlay").hidden = true;
  syncBodyScroll();
}

// ---------- opening logo reveal ----------
// Flip to true to play only on the first page of a browsing session rather than every load.
// Desktop keeps replaying it on every load; this flag is unchanged there.
const INTRO_ONCE_PER_SESSION = false;

// Touch devices always get once-per-session. The reveal is 3.3s of spinning logo, two drifting
// skyline bands and a sheen sweep with the page scroll-locked behind it — cheap enough once, but
// paying it on every single page load is a large part of why the site feels slow on a phone.
// The animation itself is untouched: same sequence, just not repeated.
function introOncePerSession() {
  return INTRO_ONCE_PER_SESSION || window.matchMedia("(hover: none)").matches;
}

function initIntro() {
  const intro = document.getElementById("intro");
  if (!intro) return;

  // Touch devices skip the reveal outright — matches the `(hover: none)` convention used for every
  // other mobile-perf decision in styles.css, rather than a width breakpoint that would also catch
  // a narrow desktop window. Bailing out here, before any listener or the fallback setTimeout below
  // is created, is what stops that timer from running for an overlay that's already hidden.
  if (window.matchMedia("(hover: none)").matches) {
    intro.hidden = true;
    return;
  }

  const alreadyPlayed = introOncePerSession() && sessionStorage.getItem("poc_intro_played") === "1";
  if (alreadyPlayed) {
    intro.hidden = true;
    return;
  }

  document.body.classList.add("intro-playing");

  const finish = () => {
    if (intro.hidden) return;
    intro.hidden = true;
    document.body.classList.remove("intro-playing");
    try {
      sessionStorage.setItem("poc_intro_played", "1");
    } catch (e) {
      // private mode — the intro just replays, which is harmless
    }
  };

  // The overlay's own fade-out is the signal it's done; the timeout is a belt-and-braces guard in
  // case animationend never fires (background tab, animations disabled at the OS level).
  intro.addEventListener("animationend", (e) => {
    if (e.animationName === "intro-out") finish();
  });
  setTimeout(finish, 1400);

  document.getElementById("intro-skip").addEventListener("click", finish);
  intro.addEventListener("click", finish);
  document.addEventListener("keydown", function onKey(e) {
    if (e.key === "Escape" || e.key === " " || e.key === "Enter") {
      finish();
      document.removeEventListener("keydown", onKey);
    }
  });
}

// ---------- newsletter signup (optional — nothing on the site is gated behind it) ----------
// Welcome + new-drop emails go out via Resend later — not wired up yet, this just persists the
// signup server-side. This was previously a full-screen gate that blocked the store until an email
// was handed over; it is now an ordinary section, and no browsing, pricing, cart or product path
// depends on it. An email is asked for once, at checkout, where it's actually needed to send a
// confirmation.
function initSignup() {
  const form = document.getElementById("signup-form");
  const errorEl = document.getElementById("signup-error");
  const successEl = document.getElementById("signup-success");
  const btn = document.getElementById("signup-submit-btn");

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const email = document.getElementById("signup-email").value.trim();
    errorEl.hidden = true;
    successEl.hidden = true;
    btn.disabled = true;
    btn.textContent = "One sec...";

    try {
      const res = await fetch("/api/subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      const data = await res.json();
      if (!data.ok) {
        errorEl.textContent = data.error || "That email didn't work — try again.";
        errorEl.hidden = false;
        btn.disabled = false;
        btn.textContent = "Sign Up";
        return;
      }
    } catch (err) {
      // Server unreachable (e.g. opened over file://). Nothing is gated on this, so the visitor
      // loses nothing — just say it didn't send rather than pretending it did.
      errorEl.textContent = "Couldn't reach the server — try again later.";
      errorEl.hidden = false;
      btn.disabled = false;
      btn.textContent = "Sign Up";
      return;
    }

    form.reset();
    successEl.hidden = false;
    btn.disabled = false;
    btn.textContent = "Sign Up";
  });
}

// ---------- vouches (buyer comments from the IG reference post) ----------
// Source: instagram.com/p/DQnkaFVkYIZ — a post that explicitly asked past buyers to vouch.
// Quotes are verbatim, including spelling and emoji. Do not reword them: these are other people's
// words, and tidying them up turns a real quote into a fabricated one.
//
// Handles are stored ALREADY MASKED, deliberately. Masking only at render time would still ship the
// real usernames in the page source, which is not anonymity. The originals live on the public post
// itself, so nothing is lost by keeping them out of this file.
const VOUCH_POST_URL = "https://www.instagram.com/p/DQnkaFVkYIZ/";

const VOUCHES = [
  { handle: "2*********",   text: "great deals and great quality 🔥" },
  { handle: "w***********", text: "always good business 💯" },
  { handle: "b************", text: "he's legit go cop for your needs 🤞🏼🤞🏼🤞🏼" },
  { handle: "d***********", text: "bro is sooo legit 🤫🤪🔥‼️" },
  { handle: "r**********",  text: "Reliable tapn ✅" },
  { handle: "i*********",   text: "🔥🔥🔥🔥 tapnnn legit" },
  { handle: "u**********",  text: "Hella clean tapn🙏😮‍💨🔥" },
  { handle: "f*******",     text: "legit 🙏🏾" },
];

// The footer is the vouch rotator: one quote at a time, swapped every VOUCH_ROTATE_MS. All the
// quotes are rendered up front and stacked, and only `.is-active` is visible, so a swap is an
// opacity/transform transition rather than a re-render.
const VOUCH_ROTATE_MS = 4000;

function initVouchFooter() {
  const rotator = document.getElementById("vouch-rotator");
  if (!rotator || VOUCHES.length === 0) return;

  rotator.innerHTML = VOUCHES.map((v, i) => `
    <span class="vouch-item${i === 0 ? " is-active" : ""}">
      <span class="vouch-quote">${v.text}</span>
      <span class="vouch-handle">@${v.handle}</span>
    </span>`).join("");

  const items = [...rotator.querySelectorAll(".vouch-item")];
  if (items.length < 2) return;

  let index = 0;
  let timer = null;

  const advance = () => {
    items[index].classList.remove("is-active");
    index = (index + 1) % items.length;
    items[index].classList.add("is-active");
  };

  const start = () => {
    // visibilitychange only fires on a change, so a page that *loads* hidden would otherwise cycle
    // through its quotes unseen; check the current state too.
    if (!timer && !document.hidden) timer = setInterval(advance, VOUCH_ROTATE_MS);
  };
  const stop = () => {
    clearInterval(timer);
    timer = null;
  };

  start();

  // Hold the quote while it's being read, and don't cycle in a background tab.
  const footer = document.getElementById("vouch-footer");
  footer.addEventListener("mouseenter", stop);
  footer.addEventListener("mouseleave", start);
  document.addEventListener("visibilitychange", () => (document.hidden ? stop() : start()));
}

// ---------- search / filter / sort controls ----------
function initControls() {
  document.getElementById("search-input").addEventListener("input", (e) => {
    state.search = e.target.value;
    renderProducts();
  });
  document.getElementById("price-min").addEventListener("input", (e) => {
    state.minPrice = e.target.value === "" ? null : Number(e.target.value);
    renderProducts();
  });
  document.getElementById("price-max").addEventListener("input", (e) => {
    state.maxPrice = e.target.value === "" ? null : Number(e.target.value);
    renderProducts();
  });
  document.getElementById("instock-checkbox").addEventListener("change", (e) => {
    state.inStockOnly = e.target.checked;
    renderProducts();
  });
  document.getElementById("sort-select").addEventListener("change", (e) => {
    state.sort = e.target.value;
    renderProducts();
  });
}

// ---------- social follow pill (tap-to-expand for touch devices) ----------
function initSocialToggle() {
  const toggle = document.getElementById("social-toggle");
  const btn = document.getElementById("social-toggle-btn");
  if (!toggle || !btn) return;
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    toggle.classList.toggle("open");
  });
  document.addEventListener("click", (e) => {
    if (!toggle.contains(e.target)) toggle.classList.remove("open");
  });
}

// ---------- 3D tilt hover, ported from the sniper-school TiltCard ----------
function initTilt() {
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduced) return;

  // Touch devices can't produce the hover this effect needs. Skipping here avoids binding 45
  // mousemove listeners and injecting 45 gradient overlays that can never be seen; the matching
  // `(hover: none)` CSS block drops the layer promotion. Desktop is unaffected.
  if (window.matchMedia("(hover: none)").matches) return;


  document.querySelectorAll(".tilt").forEach((el) => {
    if (tiltBound.has(el)) return;
    tiltBound.add(el);

    if (!el.querySelector(":scope > .tilt-glow")) {
      const glow = document.createElement("div");
      glow.className = "tilt-glow";
      el.appendChild(glow);
    }

    const maxTilt = Number(el.dataset.tiltMax || 7);
    let raf = null;

    el.addEventListener("mousemove", (e) => {
      const rect = el.getBoundingClientRect();
      const x = (e.clientX - rect.left) / rect.width;
      const y = (e.clientY - rect.top) / rect.height;

      if (raf) cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        const rx = (0.5 - y) * maxTilt * 2;
        const ry = (x - 0.5) * maxTilt * 2;
        el.style.setProperty("--rx", `${rx}deg`);
        el.style.setProperty("--ry", `${ry}deg`);
        el.style.setProperty("--spot-x", `${x * 100}%`);
        el.style.setProperty("--spot-y", `${y * 100}%`);
        el.style.setProperty("--spot-o", "0.55");
        el.style.setProperty("--scale", "1.02");
      });
    });

    el.addEventListener("mouseleave", () => {
      if (raf) {
        cancelAnimationFrame(raf);
        raf = null;
      }
      el.style.setProperty("--rx", "0deg");
      el.style.setProperty("--ry", "0deg");
      el.style.setProperty("--spot-o", "0");
      el.style.setProperty("--scale", "1");
    });
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  initIntro();
  initSignup();
  // Everything below renders prices and stock, so both files must land first. products.json and
  // the click counts don't depend on each other and share one round trip; loadPricing() has to
  // follow, because it walks PRODUCTS to stash base prices and PRODUCTS doesn't exist until
  // loadProducts() has run.
  await loadProducts();
  loadClickCounts().catch(() => {});
  await loadPricing();
  renderFeatured();
  initStack();
  renderBidCard();
  // No 6-second poll. A recurring fetch never lets iOS Safari's page-load indicator go idle, so
  // the spinner in the address bar turned forever on a phone even though the page had finished
  // rendering. Confirmed by A/B on the LAN: identical builds on two ports, the only difference
  // being this line, and the spinner stopped only on the build without it.
  //
  // The bid figure still refreshes on load and every time the page becomes visible again, which
  // is when a viewer can actually see it change. Coming back to the tab is the moment that
  // mattered anyway — nobody watches a number tick while looking at it.
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refreshBidState();
  });
  renderCategoryTiles();
  renderSizeFilter();
  renderProducts();
  initVouchFooter();
  loadCart();
  updateCartBadge();
  initModal();
  initLightbox();
  initCheckout();
  // Before initCheckoutReturn, which needs to know whether this build takes cards at all.
  await loadPaymentsConfig();
  initCart();
  initOrders();
  initControls();
  initSocialToggle();
  initTilt();
  // Last: the cart must be loaded before a paid return can clear the lines it paid for.
  initCheckoutReturn();
});


