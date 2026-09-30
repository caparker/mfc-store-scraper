-- Full price history per packaging code
SELECT
    pr.id
    , pr.packaging_code
    , p.name AS product_name
    , pk.size
    , pr.retail_dollars
    , pr.retail_unit
    , pr.wholesale_dollars
    , pr.wholesale_unit
    , pr.created_at
FROM azure.prices pr
JOIN azure.packaging pk ON pk.code = pr.packaging_code
JOIN azure.products p ON p.id = pk.products_id
ORDER BY pr.packaging_code, pr.created_at DESC
