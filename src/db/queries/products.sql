-- Products with sync state and variant count
SELECT
    p.id
    , p.name
    , p.slug
    , p.category::text AS category
    , p.shopify_product_id
    , p.shopify_status
    , p.last_changed_fields
    , count(v.id) AS variants
    , p.shopify_updated_at
    , p.updated_at
    , p.created_at
FROM azure.products p
LEFT JOIN azure.variants v ON v.products_id = p.id
GROUP BY p.id
ORDER BY p.id
