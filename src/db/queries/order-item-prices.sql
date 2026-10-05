-- List of order items and associated prices (filter by order name)
SELECT o.name as order_name
  , sku
  --, title
  , v.code as variant_code
  , variant_title
  , current_quantity
  , original_unit_price
  , discounted_total
  , retail_dollars
  , wholesale_dollars
  FROM azure.order_items i
  JOIN azure.orders o ON (o.id = i.orders_id)
  LEFT JOIN azure.variants v ON (v.id = i.variants_id)
  LEFT JOIN azure.current_prices p ON (p.variants_id = i.variants_id)
  WHERE o.name = '#2553'
  ORDER BY 1, 2
