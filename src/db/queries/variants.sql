-- Variants with product name, current price, and stock sync state
SELECT
    v.id
    , v.code
    , v.products_id
    , p.name AS product_name
    , v.size
    , v.stock
    , v.shopify_stock
    , cp.retail_dollars
    , cp.wholesale_dollars
    , v.shopify_variant_id
    , v.last_changed_fields
    , v.last_seen_at
    , v.shopify_updated_at
    , v.updated_at
FROM azure.variants v
JOIN azure.products p ON p.id = v.products_id
LEFT JOIN azure.current_prices cp ON cp.variants_id = v.id
ORDER BY v.products_id, v.code
