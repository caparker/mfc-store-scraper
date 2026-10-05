-- Full price history per variant
SELECT
    pr.id
    , pr.variants_id
    , v.code AS variant_code
    , p.name AS product_name
    , v.size
    , pr.retail_dollars
    , pr.retail_unit
    , pr.wholesale_dollars
    , pr.wholesale_unit
    , pr.created_at
FROM azure.prices pr
JOIN azure.variants v ON v.id = pr.variants_id
JOIN azure.products p ON p.id = v.products_id
ORDER BY pr.variants_id, pr.created_at DESC
