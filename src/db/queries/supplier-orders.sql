-- Placed supplier orders with variant and unit counts and total cost
SELECT
    so.id
    , so.supplier
    , so.notes
    , so.placed_at
    , count(soi.id) AS variants
    , COALESCE(sum(soi.quantity), 0) AS units
    , COALESCE(sum(soi.quantity * soi.unit_price), 0) AS total_cost
FROM azure.supplier_orders so
LEFT JOIN azure.supplier_order_items soi ON soi.supplier_orders_id = so.id
GROUP BY so.id
ORDER BY so.placed_at DESC
