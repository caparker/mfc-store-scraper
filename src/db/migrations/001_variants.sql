-- Rename azure.packaging to azure.variants and link prices, media, order_items,
-- and supplier_order_items to it by id (variants_id) instead of by code.
--
-- Apply by hand, then re-run base_schema.sql to recreate the views:
--   psql "$DB" -v ON_ERROR_STOP=1 -f src/db/migrations/001_variants.sql
--   psql "$DB" -1 -v ON_ERROR_STOP=1 -f src/db/base_schema.sql
BEGIN;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.patch_history WHERE filename = '001_variants.sql') THEN
        RAISE EXCEPTION '001_variants.sql has already been applied';
    END IF;
END
$$;

-- Every view that reads packaging or packaging_code. base_schema.sql recreates them.
DROP VIEW IF EXISTS
    public.dirty_variants
    , azure.product_search
    , azure.purchase_list
    , azure.purchase_demand
    , azure.order_item_margin
    , azure.price_latest_change
    , azure.price_total_change
    , azure.media_sync
    , azure.primary_media
    , azure.current_prices;

-- Rename the table and everything named after it.
ALTER TABLE azure.packaging RENAME TO variants;
ALTER SEQUENCE azure.packaging_id_seq RENAME TO variants_id_seq;
ALTER INDEX azure.idx_packaging_products_id RENAME TO idx_variants_products_id;
ALTER TRIGGER packaging_set_updated_at ON azure.variants RENAME TO variants_set_updated_at;

DO $$
DECLARE
    c record;
BEGIN
    FOR c IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'azure.variants'::regclass
          AND conname LIKE 'packaging\_%'
    LOOP
        EXECUTE format(
            'ALTER TABLE azure.variants RENAME CONSTRAINT %I TO %I'
            , c.conname
            , 'variants_' || substr(c.conname, length('packaging_') + 1)
        );
    END LOOP;
END
$$;

-- The live tables were created without the primary keys base_schema.sql
-- declares. variants needs one for the foreign keys below.
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['variants', 'prices', 'media'] LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = format('azure.%I', t)::regclass AND contype = 'p'
        ) THEN
            EXECUTE format('ALTER TABLE azure.%I ADD PRIMARY KEY (id)', t);
        END IF;
    END LOOP;
END
$$;

-- base_schema.sql declares UNIQUE(products_id, size). Add it when the data
-- allows; otherwise report the duplicates and leave it for a later cleanup.
DO $$
DECLARE
    dupes integer;
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'azure.variants'::regclass
          AND conname = 'variants_products_id_size_key'
    ) THEN
        RETURN;
    END IF;
    SELECT count(*) INTO dupes
    FROM (SELECT 1 FROM azure.variants GROUP BY products_id, size HAVING count(*) > 1) d;
    IF dupes = 0 THEN
        ALTER TABLE azure.variants ADD CONSTRAINT variants_products_id_size_key UNIQUE (products_id, size);
    ELSE
        RAISE NOTICE 'variants: % (products_id, size) duplicate group(s); UNIQUE(products_id, size) not added', dupes;
    END IF;
END
$$;

-- prices: packaging_code -> variants_id. The live table carries a stray
-- updated_at trigger from an early schema; prices has no updated_at column, so
-- it fails on any UPDATE.
DROP TRIGGER IF EXISTS set_timestamp_update_prices ON azure.prices;
ALTER TABLE azure.prices ADD COLUMN variants_id INTEGER;
UPDATE azure.prices pr
SET variants_id = v.id
FROM azure.variants v
WHERE v.code = pr.packaging_code;
ALTER TABLE azure.prices
    ALTER COLUMN variants_id SET NOT NULL
    , ADD CONSTRAINT prices_variants_id_fkey
        FOREIGN KEY (variants_id) REFERENCES azure.variants(id) ON DELETE CASCADE
    , DROP COLUMN packaging_code;
CREATE INDEX idx_prices_variants_id_created ON azure.prices (variants_id, created_at DESC);

-- media: packaging_code -> variants_id
ALTER TABLE azure.media ADD COLUMN variants_id INTEGER;
UPDATE azure.media m
SET variants_id = v.id
FROM azure.variants v
WHERE v.code = m.packaging_code;
ALTER TABLE azure.media
    ALTER COLUMN variants_id SET NOT NULL
    , ADD CONSTRAINT media_variants_id_fkey
        FOREIGN KEY (variants_id) REFERENCES azure.variants(id) ON DELETE CASCADE
    , ADD CONSTRAINT media_variants_id_original_url_key UNIQUE (variants_id, original_url)
    , DROP COLUMN packaging_code;
CREATE INDEX idx_media_variants_id ON azure.media (variants_id);

-- order_items: packaging_code -> variants_id (nullable; sku is kept for relinking)
ALTER TABLE azure.order_items ADD COLUMN variants_id INTEGER;
UPDATE azure.order_items oi
SET variants_id = v.id
FROM azure.variants v
WHERE v.code = oi.packaging_code;
ALTER TABLE azure.order_items
    ADD CONSTRAINT order_items_variants_id_fkey
        FOREIGN KEY (variants_id) REFERENCES azure.variants(id) ON DELETE SET NULL
    , DROP COLUMN packaging_code;
CREATE INDEX idx_order_items_variants_id ON azure.order_items (variants_id);

-- supplier_order_items: packaging_code -> variants_id (nullable; sku identifies non-Azure items)
ALTER TABLE azure.supplier_order_items ADD COLUMN variants_id INTEGER;
UPDATE azure.supplier_order_items soi
SET variants_id = v.id
FROM azure.variants v
WHERE v.code = soi.packaging_code;
ALTER TABLE azure.supplier_order_items
    ADD CONSTRAINT supplier_order_items_variants_id_fkey
        FOREIGN KEY (variants_id) REFERENCES azure.variants(id) ON DELETE SET NULL
    , DROP COLUMN packaging_code;
CREATE INDEX idx_supplier_order_items_variants_id ON azure.supplier_order_items (variants_id);

INSERT INTO public.patch_history (filename) VALUES ('001_variants.sql');

COMMIT;
