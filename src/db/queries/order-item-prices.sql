-- List of order items and associated prices (filter by order name)
SELECT o.name as order_name
  , sku
  --, title
  , i.packaging_code as variant_code
  , variant_title
  , current_quantity
  , original_unit_price
  , discounted_total
  , retail_dollars
  , wholesale_dollars
  FROM azure.order_items i
  JOIN azure.orders o ON (o.id = i.orders_id)
  LEFT JOIN azure.current_prices p ON (p.packaging_code = i.packaging_code)
  WHERE o.name = '#2553'
  ORDER BY 1, 2
