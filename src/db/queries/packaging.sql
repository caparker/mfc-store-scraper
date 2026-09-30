-- Packaging (variants) with product name, current price, and stock sync state
SELECT
    pk.id
    , pk.code
    , pk.products_id
    , p.name AS product_name
    , pk.size
    , pk.stock
    , pk.shopify_stock
    , cp.retail_dollars
    , cp.wholesale_dollars
    , pk.shopify_variant_id
    , pk.last_changed_fields
    , pk.last_seen_at
    , pk.shopify_updated_at
    , pk.updated_at
FROM azure.packaging pk
JOIN azure.products p ON p.id = pk.products_id
LEFT JOIN azure.current_prices cp ON cp.packaging_code = pk.code
ORDER BY pk.products_id, pk.code
