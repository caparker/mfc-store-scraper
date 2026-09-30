-- Order line items with order name and status
SELECT
    oi.id
    , o.name AS order_name
    , o.financial_status
    , o.fulfillment_status
    , oi.packaging_code
    , oi.sku
    , oi.title
    , oi.variant_title
    , oi.quantity
    , oi.unfulfilled_quantity
    , oi.original_unit_price
    , o.ordered_at
FROM azure.order_items oi
JOIN azure.orders o ON o.id = oi.orders_id
ORDER BY o.ordered_at DESC, oi.id
