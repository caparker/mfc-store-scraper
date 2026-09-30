-- All mirrored orders with customer email
SELECT
    o.id
    , o.name
    , c.email
    , o.financial_status
    , o.fulfillment_status
    , o.ordered_at
    , o.cancelled_at
    , o.closed_at
    , o.subtotal
    , o.total_discounts
    , o.total_shipping
    , o.total_tax
    , o.total
    , o.last_pulled_at
FROM azure.orders o
LEFT JOIN azure.customers c ON c.id = o.customers_id
ORDER BY o.ordered_at DESC
