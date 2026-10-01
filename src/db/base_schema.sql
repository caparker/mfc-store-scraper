CREATE EXTENSION IF NOT EXISTS ltree;

-- TABLE DEFINITIONS
-- Patch History Table
CREATE TABLE IF NOT EXISTS public.patch_history (
    filename TEXT PRIMARY KEY,
    created TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Set updated_at during updates
CREATE OR REPLACE FUNCTION trigger_set_timestamp()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION set_updated_at_if_changed()
RETURNS TRIGGER AS $$
DECLARE
    ignored_cols text[] := TG_ARGV::text[];
    new_json jsonb := to_jsonb(NEW);
    old_json jsonb := to_jsonb(OLD);
    col text;
    changed text[] := ARRAY[]::text[];
    k text;
BEGIN
    FOREACH col IN ARRAY ignored_cols LOOP
        new_json := new_json - col;
        old_json := old_json - col;
    END LOOP;

    -- Collect keys whose values differ (null-safe).
    FOR k IN SELECT jsonb_object_keys(new_json || old_json) LOOP
        IF (new_json -> k) IS DISTINCT FROM (old_json -> k) THEN
            changed := array_append(changed, k);
        END IF;
    END LOOP;

    IF array_length(changed, 1) IS NULL THEN
        -- Nothing meaningful changed — preserve prior bookkeeping.
        NEW.updated_at = OLD.updated_at;
        NEW.last_changed_fields = OLD.last_changed_fields;
    ELSE
        NEW.updated_at = now();
        NEW.last_changed_fields = changed;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


CREATE SCHEMA IF NOT EXISTS azure;

CREATE TABLE IF NOT EXISTS azure.products (
    id INTEGER PRIMARY KEY NOT NULL,
    shopify_product_id TEXT,
    name TEXT,
    short_description TEXT,
    description TEXT,
    slug TEXT UNIQUE,
    storage_climate TEXT,
    unshippable_regions JSONB,
    brand JSONB,
    substitutions JSONB,
    category ltree,
    last_changed_fields TEXT[],
    shopify_status TEXT NOT NULL DEFAULT 'DRAFT',
    shopify_updated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Existing databases were created before shopify_status existed.
ALTER TABLE azure.products
    ADD COLUMN IF NOT EXISTS shopify_status TEXT NOT NULL DEFAULT 'DRAFT';

CREATE OR REPLACE TRIGGER products_set_updated_at
BEFORE UPDATE ON azure.products
FOR EACH ROW
EXECUTE FUNCTION set_updated_at_if_changed(
    'updated_at', 'created_at',
    'shopify_updated_at', 'shopify_product_id',
    'last_changed_fields'
);


CREATE TABLE IF NOT EXISTS azure.packaging (
    id SERIAL PRIMARY KEY,
    products_id INTEGER NOT NULL REFERENCES azure.products(id) ON DELETE CASCADE,
    code TEXT,
    shopify_variant_id TEXT,
    shopify_inventory_item_id TEXT,
    size TEXT,
    weight JSONB,
    stock INTEGER DEFAULT 0,
    rewards_enabled BOOLEAN DEFAULT FALSE,
    freight_handling_required BOOLEAN DEFAULT FALSE,
    tags JSONB,
    primary_category INTEGER,
    favorites INTEGER,
    next_purchase_arrival TIMESTAMPTZ,
    last_changed_fields TEXT[],
    -- Last time a scrape returned this row; rows absent from a full scrape get stock = 0.
    last_seen_at TIMESTAMPTZ,
    -- Last stock value pushed to Shopify; stock is dirty when it differs.
    shopify_stock INTEGER,
    shopify_updated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(code),
    UNIQUE(products_id, size)
);

-- Existing databases were created before these columns existed.
ALTER TABLE azure.packaging
    ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ
    , ADD COLUMN IF NOT EXISTS shopify_stock INTEGER;

-- Only columns that reach Shopify mark a variant dirty: in practice `size`
-- (the option value) plus a new price row. stock has its own sync (stock vs
-- shopify_stock); every other column here is never sent to Shopify.
CREATE OR REPLACE TRIGGER packaging_set_updated_at
BEFORE UPDATE ON azure.packaging
FOR EACH ROW
EXECUTE FUNCTION set_updated_at_if_changed(
    'updated_at', 'created_at',
    'shopify_updated_at', 'shopify_variant_id', 'shopify_inventory_item_id',
    'last_changed_fields',
    'stock', 'shopify_stock', 'next_purchase_arrival', 'last_seen_at',
    'tags', 'weight', 'favorites',
    'primary_category', 'rewards_enabled', 'freight_handling_required'
);


CREATE TABLE IF NOT EXISTS azure.prices (
    id SERIAL PRIMARY KEY,
    packaging_code TEXT NOT NULL REFERENCES azure.packaging(code) ON DELETE CASCADE,
    retail_dollars REAL,
    retail_unit TEXT,
    wholesale_dollars REAL,
    wholesale_unit TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_prices_packaging_code_created
  ON azure.prices (packaging_code, created_at DESC);

-- One row per (packaging, image url) as listed by Azure, in Azure's order.
-- Only the first image of each packaging (see azure.primary_media) is pushed to
-- Shopify: it is created on the product and set as the variant image. The
-- Shopify side is tracked explicitly rather than by updated_at:
--   shopify_media_id / shopify_status  the media created on the product
--                                      (UPLOADED, PROCESSING, READY, FAILED)
--   variant_media_set_at               when the variant was pointed at it
--   error / attempts / last_attempt_at what went wrong and how often
CREATE TABLE IF NOT EXISTS azure.media (
    id SERIAL PRIMARY KEY,
    packaging_code TEXT NOT NULL REFERENCES azure.packaging(code) ON DELETE CASCADE,
    original_url TEXT NOT NULL,
    file_name TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    shopify_media_id TEXT,
    shopify_status TEXT,
    variant_media_set_at TIMESTAMPTZ,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMPTZ,
    shopify_updated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(packaging_code, original_url)
);

-- Existing databases were created before these columns existed.
ALTER TABLE azure.media
    ADD COLUMN IF NOT EXISTS position INTEGER NOT NULL DEFAULT 0
    , ADD COLUMN IF NOT EXISTS shopify_status TEXT
    , ADD COLUMN IF NOT EXISTS variant_media_set_at TIMESTAMPTZ
    , ADD COLUMN IF NOT EXISTS error TEXT
    , ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0
    , ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ;

-- An earlier version attached this trigger to azure.packaging by mistake.
DROP TRIGGER IF EXISTS media_set_updated_at ON azure.packaging;

CREATE INDEX IF NOT EXISTS idx_packaging_products_id ON azure.packaging(products_id);
CREATE INDEX IF NOT EXISTS idx_media_packaging_code ON azure.media(packaging_code);

-- The image pushed to Shopify for each packaging: first in Azure's order.
CREATE OR REPLACE VIEW azure.primary_media AS
SELECT DISTINCT ON (packaging_code) *
FROM azure.media
ORDER BY packaging_code, position, id;

-- Primary media for variants that exist in Shopify, with a sync state:
--   done     variant points at the media
--   failed   Shopify rejected the image, or three attempts have failed
--   pending  everything else
CREATE OR REPLACE VIEW azure.media_sync AS
SELECT
    m.*
    , pack.id AS packaging_id
    , pack.shopify_variant_id
    , prod.id AS product_id
    , prod.shopify_product_id
    , prod.name AS product_name
    , CASE
        WHEN m.variant_media_set_at IS NOT NULL THEN 'done'
        WHEN m.shopify_status = 'FAILED' OR m.attempts >= 3 THEN 'failed'
        ELSE 'pending'
      END AS sync_state
FROM azure.primary_media m
JOIN azure.packaging pack ON pack.code = m.packaging_code
JOIN azure.products prod ON prod.id = pack.products_id
WHERE pack.shopify_variant_id IS NOT NULL
  AND prod.shopify_product_id IS NOT NULL
  AND prod.shopify_status <> 'DELETED';

CREATE OR REPLACE VIEW azure.current_prices AS
SELECT DISTINCT ON (packaging_code) *
FROM azure.prices
ORDER BY packaging_code, created_at DESC;


CREATE OR REPLACE VIEW azure.price_total_change AS
SELECT
    pk.products_id,
    pk.code AS packaging_code,
    first_price.retail_dollars AS first_retail_dollars,
    last_price.retail_dollars AS current_retail_dollars,
    (last_price.retail_dollars - first_price.retail_dollars) AS retail_change,
    first_price.wholesale_dollars AS first_wholesale_dollars,
    last_price.wholesale_dollars AS current_wholesale_dollars,
    (last_price.wholesale_dollars - first_price.wholesale_dollars) AS wholesale_change,
    first_price.created_at AS first_recorded_at,
    last_price.created_at AS last_recorded_at
FROM azure.packaging pk
JOIN LATERAL (
    SELECT retail_dollars, wholesale_dollars, created_at
    FROM azure.prices
    WHERE packaging_code = pk.code
    ORDER BY created_at ASC
    LIMIT 1
) first_price ON TRUE
JOIN LATERAL (
    SELECT retail_dollars, wholesale_dollars, created_at
    FROM azure.prices
    WHERE packaging_code = pk.code
    ORDER BY created_at DESC
    LIMIT 1
) last_price ON TRUE;


CREATE OR REPLACE VIEW azure.price_latest_change AS
WITH ranked AS (
    SELECT
        packaging_code,
        retail_dollars,
        wholesale_dollars,
        created_at,
        ROW_NUMBER() OVER (PARTITION BY packaging_code ORDER BY created_at DESC) AS rn
    FROM azure.prices
)
SELECT
    pk.products_id,
    p.shopify_product_id,
    pk.shopify_variant_id,
    curr.packaging_code,
    prev.retail_dollars AS previous_retail_dollars,
    curr.retail_dollars AS current_retail_dollars,
    (curr.retail_dollars - prev.retail_dollars) AS retail_change,
    prev.wholesale_dollars AS previous_wholesale_dollars,
    curr.wholesale_dollars AS current_wholesale_dollars,
    (curr.wholesale_dollars - prev.wholesale_dollars) AS wholesale_change,
    curr.created_at AS changed_at,
    (prev.created_at - curr.created_at) AS age
FROM ranked curr
JOIN azure.packaging pk ON pk.code = curr.packaging_code
JOIN azure.products p ON (pk.products_id = p.id)
LEFT JOIN ranked prev
    ON prev.packaging_code = curr.packaging_code AND prev.rn = 2
WHERE curr.rn = 1
    AND prev.retail_dollars IS NOT NULL;;

CREATE OR REPLACE VIEW dirty_products AS
SELECT
    id AS products_id,
    shopify_product_id,
    name,
    updated_at,
    shopify_updated_at,
    last_changed_fields
FROM azure.products
WHERE shopify_product_id IS NOT NULL
  AND (shopify_updated_at IS NULL OR shopify_updated_at < updated_at)
ORDER BY updated_at DESC;

CREATE OR REPLACE VIEW pending_products AS
SELECT
    id AS products_id,
    name,
    created_at
FROM azure.products
WHERE shopify_product_id IS NULL
ORDER BY created_at DESC;

CREATE OR REPLACE VIEW dirty_variants AS
WITH latest_price AS (
    SELECT DISTINCT ON (packaging_code)
        packaging_code, retail_dollars, created_at
    FROM azure.prices
    ORDER BY packaging_code, created_at DESC
)
SELECT
    prod.id                 AS products_id,
    prod.shopify_product_id,
    pack.id                 AS packaging_id,
    pack.code               AS packaging_code,
    pack.stock,
    pack.shopify_variant_id,
    lp.retail_dollars       AS latest_price,
    lp.created_at           AS latest_price_at,
    pack.updated_at         AS packaging_updated_at,
    pack.shopify_updated_at,
    pack.last_changed_fields
FROM azure.packaging pack
JOIN azure.products prod ON prod.id = pack.products_id
LEFT JOIN latest_price lp ON lp.packaging_code = pack.code
WHERE pack.shopify_variant_id IS NOT NULL
  AND prod.shopify_product_id IS NOT NULL
  AND lp.retail_dollars IS NOT NULL
  AND (
    pack.shopify_updated_at IS NULL
    OR pack.shopify_updated_at < GREATEST(pack.updated_at, lp.created_at)
  )
ORDER BY GREATEST(pack.updated_at, lp.created_at) DESC;


CREATE OR REPLACE VIEW azure.product_search AS
SELECT
    p.id AS products_id,
    p.shopify_product_id,
    p.name AS product_name,
    p.slug,
    p.category,
    pk.id AS packaging_id,
    pk.code AS packaging_code,
    pk.shopify_variant_id,
    pk.size,
    pk.stock,
    cp.retail_dollars,
    cp.retail_unit,
    cp.wholesale_dollars,
    cp.wholesale_unit,
    cp.created_at AS price_recorded_at
FROM azure.products p
JOIN azure.packaging pk ON pk.products_id = p.id
LEFT JOIN azure.current_prices cp ON cp.packaging_code = pk.code;


-- CUSTOMERS AND ORDERS
--
-- Customers: Shopify owns the contact columns (they are pulled and never
-- pushed after the initial create); the DB owns the membership columns,
-- which are pushed to Shopify as metafields under the `membership`
-- namespace. Only the membership columns mark a row dirty, so a pull never
-- dirties a customer and a push never touches contact info.
CREATE TABLE IF NOT EXISTS azure.customers (
    id SERIAL PRIMARY KEY,
    shopify_customer_id TEXT UNIQUE,
    email TEXT UNIQUE,
    first_name TEXT,
    last_name TEXT,
    phone TEXT,
    default_address JSONB,
    -- Membership (local source of truth, pushed to Shopify)
    member_number TEXT UNIQUE,
    membership_status TEXT,
    member_since DATE,
    membership_expires DATE,
    -- Local only, never pushed
    notes TEXT,
    last_changed_fields TEXT[],
    -- Shopify's updatedAt for this customer as of the last pull; the next
    -- pull only asks for customers updated since the newest value seen.
    remote_updated_at TIMESTAMPTZ,
    shopify_updated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE OR REPLACE TRIGGER customers_set_updated_at
BEFORE UPDATE ON azure.customers
FOR EACH ROW
EXECUTE FUNCTION set_updated_at_if_changed(
    'updated_at', 'created_at',
    'shopify_customer_id', 'shopify_updated_at', 'remote_updated_at',
    'last_changed_fields',
    'email', 'first_name', 'last_name', 'phone', 'default_address',
    'notes'
);

-- Orders are read-only mirrors of Shopify. pull-orders upserts every open
-- unfulfilled order and refreshes any locally open order that Shopify no
-- longer lists as open.
CREATE TABLE IF NOT EXISTS azure.orders (
    id SERIAL PRIMARY KEY,
    shopify_order_id TEXT UNIQUE NOT NULL,
    name TEXT,
    customers_id INTEGER REFERENCES azure.customers(id) ON DELETE SET NULL,
    shopify_customer_id TEXT,
    financial_status TEXT,
    fulfillment_status TEXT,
    ordered_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    note TEXT,
    -- Money in shop currency, as of the last pull (orders are only re-pulled while open).
    currency TEXT,
    subtotal NUMERIC(12, 2),
    total_tax NUMERIC(12, 2),
    total_discounts NUMERIC(12, 2),
    total_shipping NUMERIC(12, 2),
    total NUMERIC(12, 2),
    net_payment NUMERIC(12, 2),
    total_refunded NUMERIC(12, 2),
    -- Shopify's updatedAt as of the last pull; the next pull also asks for
    -- orders updated since the newest value seen, open or closed.
    remote_updated_at TIMESTAMPTZ,
    last_pulled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Existing databases were created before these columns existed.
ALTER TABLE azure.orders
    ADD COLUMN IF NOT EXISTS currency TEXT
    , ADD COLUMN IF NOT EXISTS subtotal NUMERIC(12, 2)
    , ADD COLUMN IF NOT EXISTS total_tax NUMERIC(12, 2)
    , ADD COLUMN IF NOT EXISTS total_discounts NUMERIC(12, 2)
    , ADD COLUMN IF NOT EXISTS total_shipping NUMERIC(12, 2)
    , ADD COLUMN IF NOT EXISTS total NUMERIC(12, 2)
    , ADD COLUMN IF NOT EXISTS net_payment NUMERIC(12, 2)
    , ADD COLUMN IF NOT EXISTS total_refunded NUMERIC(12, 2)
    , ADD COLUMN IF NOT EXISTS remote_updated_at TIMESTAMPTZ;

CREATE OR REPLACE TRIGGER orders_set_timestamp
BEFORE UPDATE ON azure.orders
FOR EACH ROW
EXECUTE FUNCTION trigger_set_timestamp();

CREATE TABLE IF NOT EXISTS azure.order_items (
    id SERIAL PRIMARY KEY,
    orders_id INTEGER NOT NULL REFERENCES azure.orders(id) ON DELETE CASCADE,
    shopify_line_item_id TEXT UNIQUE NOT NULL,
    shopify_variant_id TEXT,
    sku TEXT,
    -- Derived from an `AZ-<code>` SKU; NULL for anything that is not an Azure product.
    packaging_code TEXT REFERENCES azure.packaging(code) ON DELETE SET NULL,
    title TEXT,
    variant_title TEXT,
    -- quantity is what was ordered; current_quantity is what remains after
    -- edits and refunds. cancelled_quantity was removed before fulfillment
    -- (refund restockType CANCEL); returned_quantity was refunded after.
    quantity INTEGER NOT NULL DEFAULT 0,
    current_quantity INTEGER,
    unfulfilled_quantity INTEGER NOT NULL DEFAULT 0,
    cancelled_quantity INTEGER NOT NULL DEFAULT 0,
    returned_quantity INTEGER NOT NULL DEFAULT 0,
    status TEXT GENERATED ALWAYS AS (
        CASE
            WHEN COALESCE(current_quantity, quantity) = 0 AND returned_quantity > 0 THEN 'returned'
            WHEN COALESCE(current_quantity, quantity) = 0 THEN 'removed'
            WHEN COALESCE(current_quantity, quantity) < quantity THEN 'partial'
            WHEN unfulfilled_quantity = 0 THEN 'fulfilled'
            ELSE 'open'
        END
    ) STORED,
    -- Shop currency. discounted_* include allocated order-level discounts.
    -- discounted_total is Shopify's figure for the ordered quantity; it is not
    -- reduced when items are removed. Use discounted_unit_price * current_quantity.
    original_unit_price NUMERIC(12, 2),
    discounted_unit_price NUMERIC(12, 2),
    discounted_total NUMERIC(12, 2),
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE azure.order_items
    ADD COLUMN IF NOT EXISTS original_unit_price NUMERIC(12, 2)
    , ADD COLUMN IF NOT EXISTS discounted_unit_price NUMERIC(12, 2)
    , ADD COLUMN IF NOT EXISTS discounted_total NUMERIC(12, 2)
    , ADD COLUMN IF NOT EXISTS current_quantity INTEGER
    , ADD COLUMN IF NOT EXISTS cancelled_quantity INTEGER NOT NULL DEFAULT 0
    , ADD COLUMN IF NOT EXISTS returned_quantity INTEGER NOT NULL DEFAULT 0
    , ADD COLUMN IF NOT EXISTS status TEXT GENERATED ALWAYS AS (
        CASE
            WHEN COALESCE(current_quantity, quantity) = 0 AND returned_quantity > 0 THEN 'returned'
            WHEN COALESCE(current_quantity, quantity) = 0 THEN 'removed'
            WHEN COALESCE(current_quantity, quantity) < quantity THEN 'partial'
            WHEN unfulfilled_quantity = 0 THEN 'fulfilled'
            ELSE 'open'
        END
    ) STORED;

CREATE OR REPLACE TRIGGER order_items_set_timestamp
BEFORE UPDATE ON azure.order_items
FOR EACH ROW
EXECUTE FUNCTION trigger_set_timestamp();

CREATE INDEX IF NOT EXISTS idx_orders_customers_id ON azure.orders(customers_id);
CREATE INDEX IF NOT EXISTS idx_order_items_orders_id ON azure.order_items(orders_id);
CREATE INDEX IF NOT EXISTS idx_order_items_packaging_code ON azure.order_items(packaging_code);

-- A supplier order is a snapshot of demand that was actually purchased from a
-- supplier. Recording it lets the purchase list show only net new demand.
CREATE TABLE IF NOT EXISTS azure.supplier_orders (
    id SERIAL PRIMARY KEY,
    supplier TEXT NOT NULL DEFAULT 'azure',
    placed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- The first version of supplier_order_items was one row per customer line
-- item with no price. Drop it (and the views that depend on it) so the
-- per-variant table below can be created; the data it held was never used.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'azure'
          AND table_name = 'supplier_order_items'
          AND column_name = 'order_items_id'
    ) THEN
        DROP VIEW IF EXISTS azure.purchase_list;
        DROP VIEW IF EXISTS azure.purchase_demand;
        DROP TABLE azure.supplier_order_items;
    END IF;
END
$$;

-- One row per variant purchased. unit_price is what was paid per packaging
-- item; it defaults to the Azure retail price current when the order was
-- committed and can be corrected from the invoice. Non-Azure items have no
-- packaging_code and are identified by sku.
CREATE TABLE IF NOT EXISTS azure.supplier_order_items (
    id SERIAL PRIMARY KEY,
    supplier_orders_id INTEGER NOT NULL REFERENCES azure.supplier_orders(id) ON DELETE CASCADE,
    packaging_code TEXT REFERENCES azure.packaging(code) ON DELETE SET NULL,
    sku TEXT,
    quantity INTEGER NOT NULL,
    unit_price NUMERIC(12, 2)
);

CREATE INDEX IF NOT EXISTS idx_supplier_order_items_supplier_orders_id
  ON azure.supplier_order_items(supplier_orders_id);
CREATE INDEX IF NOT EXISTS idx_supplier_order_items_packaging_code
  ON azure.supplier_order_items(packaging_code);

-- Which customer line items a supplier order item covers, and how many units
-- of each. A line item can be split across supplier orders when the first
-- purchase was short.
CREATE TABLE IF NOT EXISTS azure.supplier_order_allocations (
    id SERIAL PRIMARY KEY,
    supplier_order_items_id INTEGER NOT NULL REFERENCES azure.supplier_order_items(id) ON DELETE CASCADE,
    order_items_id INTEGER NOT NULL REFERENCES azure.order_items(id) ON DELETE CASCADE,
    quantity INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_supplier_order_allocations_order_items_id
  ON azure.supplier_order_allocations(order_items_id);
CREATE INDEX IF NOT EXISTS idx_supplier_order_allocations_supplier_order_items_id
  ON azure.supplier_order_allocations(supplier_order_items_id);

CREATE OR REPLACE VIEW azure.open_orders AS
SELECT *
FROM azure.orders
WHERE cancelled_at IS NULL
  AND closed_at IS NULL
  AND fulfillment_status IS DISTINCT FROM 'FULFILLED';

-- Per line item: how many units are still unfulfilled and not yet on a
-- supplier order. Fulfilling from stock or cancelling after a supplier order
-- was placed can push the raw number negative; it is clamped at 0.
CREATE OR REPLACE VIEW azure.purchase_demand AS
SELECT
    oi.id AS order_items_id
    , o.id AS orders_id
    , o.name AS order_name
    , o.ordered_at
    , o.financial_status
    , oi.packaging_code
    , oi.sku
    , oi.title
    , oi.variant_title
    , oi.unfulfilled_quantity
    , COALESCE(soa.ordered, 0) AS supplier_ordered
    , GREATEST(oi.unfulfilled_quantity - COALESCE(soa.ordered, 0), 0) AS outstanding
FROM azure.order_items oi
JOIN azure.open_orders o ON o.id = oi.orders_id
LEFT JOIN LATERAL (
    SELECT sum(quantity) AS ordered
    FROM azure.supplier_order_allocations
    WHERE order_items_id = oi.id
) soa ON TRUE
WHERE oi.unfulfilled_quantity > 0;

-- Per packaging code: what to buy from Azure. Items without a packaging code
-- (non-Azure products) are grouped by SKU and title instead. retail_dollars is
-- the price recorded on the supplier order when demand is committed.
-- Dropped first: CREATE OR REPLACE cannot add a column mid-view.
DROP VIEW IF EXISTS azure.purchase_list;
CREATE VIEW azure.purchase_list AS
SELECT
    pd.packaging_code
    , pd.sku
    , COALESCE(p.name, pd.title) AS product_name
    , COALESCE(pk.size, pd.variant_title) AS size
    , p.id AS products_id
    , pk.stock AS azure_stock
    , cp.retail_dollars
    , cp.wholesale_dollars
    , cp.wholesale_unit
    , sum(pd.outstanding) AS outstanding
    , count(DISTINCT pd.orders_id) AS order_count
    , min(pd.ordered_at) AS oldest_order_at
FROM azure.purchase_demand pd
LEFT JOIN azure.packaging pk ON pk.code = pd.packaging_code
LEFT JOIN azure.products p ON p.id = pk.products_id
LEFT JOIN azure.current_prices cp ON cp.packaging_code = pd.packaging_code
WHERE pd.outstanding > 0
GROUP BY
    pd.packaging_code, pd.sku, p.name, pd.title, pk.size, pd.variant_title
    , p.id, pk.stock, cp.retail_dollars, cp.wholesale_dollars, cp.wholesale_unit
ORDER BY pd.packaging_code IS NULL, product_name, size;

-- Per customer line item: units bought from a supplier and what they cost,
-- taken from the supplier order items that cover it. Lines never purchased
-- (fulfilled from stock, or not yet ordered) are absent.
CREATE OR REPLACE VIEW azure.order_item_cost AS
SELECT
    soa.order_items_id
    , sum(soa.quantity) AS purchased_quantity
    , sum(soa.quantity * soi.unit_price) AS purchased_cost
    , CASE
        WHEN sum(soa.quantity) > 0
        THEN round(sum(soa.quantity * soi.unit_price) / sum(soa.quantity), 2)
      END AS unit_cost
    , min(so.placed_at) AS first_purchased_at
    , max(so.placed_at) AS last_purchased_at
FROM azure.supplier_order_allocations soa
JOIN azure.supplier_order_items soi ON soi.id = soa.supplier_order_items_id
JOIN azure.supplier_orders so ON so.id = soi.supplier_orders_id
GROUP BY soa.order_items_id;

-- Margin per sold line: what the customer paid per unit against the Azure
-- wholesale and retail prices current at query time (not the price on the
-- purchase date). Uses current_quantity, so removed and returned units do not
-- count as sold.
-- Dropped first: CREATE OR REPLACE cannot change a view column's type.
DROP VIEW IF EXISTS azure.order_item_margin;
CREATE VIEW azure.order_item_margin AS
SELECT
    o.id AS orders_id
    , o.name AS order_name
    , o.ordered_at
    , o.fulfillment_status
    , oi.id AS order_items_id
    , oi.packaging_code
    , oi.sku
    , oi.title
    , oi.variant_title
    , oi.status
    , oi.quantity
    , q.current_quantity
    , oi.original_unit_price
    , oi.discounted_unit_price
    , (oi.discounted_unit_price * q.current_quantity) AS line_total
    , cp.retail_dollars
    , cp.retail_unit
    , cp.wholesale_dollars
    , cp.wholesale_unit
    , (oi.discounted_unit_price - cp.wholesale) AS wholesale_unit_margin
    , ((oi.discounted_unit_price - cp.wholesale) * q.current_quantity) AS wholesale_line_margin
    , (oi.discounted_unit_price - cp.retail) AS retail_unit_margin
    , ((oi.discounted_unit_price - cp.retail) * q.current_quantity) AS retail_line_margin
    , CASE
        WHEN oi.discounted_unit_price > 0
        THEN round((oi.discounted_unit_price - cp.wholesale) / oi.discounted_unit_price * 100, 1)
      END AS margin_percent
FROM azure.order_items oi
JOIN azure.orders o ON o.id = oi.orders_id
CROSS JOIN LATERAL (
    SELECT COALESCE(oi.current_quantity, oi.quantity) AS current_quantity
) q
LEFT JOIN LATERAL (
    SELECT wholesale_dollars
      , wholesale_unit
      , wholesale_dollars::numeric(12, 2) AS wholesale
      , retail_dollars
      , retail_unit
      , retail_dollars::numeric(12, 2) AS retail
    FROM azure.current_prices
    WHERE packaging_code = oi.packaging_code
) cp ON TRUE
WHERE o.cancelled_at IS NULL;





CREATE OR REPLACE VIEW dirty_customers AS
SELECT
    id AS customers_id
    , shopify_customer_id
    , email
    , member_number
    , updated_at
    , shopify_updated_at
    , last_changed_fields
FROM azure.customers
WHERE shopify_customer_id IS NOT NULL
  AND (shopify_updated_at IS NULL OR shopify_updated_at < updated_at)
ORDER BY updated_at DESC;

CREATE OR REPLACE VIEW pending_customers AS
SELECT
    id AS customers_id
    , email
    , member_number
    , created_at
FROM azure.customers
WHERE shopify_customer_id IS NULL
ORDER BY created_at DESC;
