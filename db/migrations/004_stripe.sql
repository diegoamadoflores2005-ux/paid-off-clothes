-- Stripe Checkout: link an order to the session that pays for it.
--
-- The webhook arrives knowing only Stripe's own ids, so the lookup has to work in that direction.
-- payment_ref holds the Checkout Session id (cs_...) and payment_intent the PaymentIntent (pi_...);
-- a refund event names the PaymentIntent, not the session, which is why both are stored rather
-- than just the one that created the order.
--
-- Nothing here replaces payment_events: that table is still what makes a replayed webhook a no-op.
-- These columns only answer "which order is this Stripe object about".

ALTER TABLE orders ADD COLUMN payment_provider TEXT NOT NULL DEFAULT '';
ALTER TABLE orders ADD COLUMN payment_ref TEXT;            -- Checkout Session id
ALTER TABLE orders ADD COLUMN payment_intent TEXT;         -- PaymentIntent id
ALTER TABLE orders ADD COLUMN payment_mode TEXT NOT NULL DEFAULT '';  -- test | live

-- Partial indexes: only paid-for orders carry these, and NULLs would otherwise collide under a
-- plain UNIQUE index.
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_payment_ref
  ON orders(payment_ref) WHERE payment_ref IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_payment_intent
  ON orders(payment_intent) WHERE payment_intent IS NOT NULL;
