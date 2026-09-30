-- Placed supplier orders with item and unit counts
SELECT
    so.id
    , so.supplier
    , so.notes
    , so.placed_at
    , count(soi.id) AS items
    , COALESCE(sum(soi.quantity), 0) AS units
FROM azure.supplier_orders so
LEFT JOIN azure.supplier_order_items soi ON soi.supplier_orders_id = so.id
GROUP BY so.id
ORDER BY so.placed_at DESC
