-- Per line item retail vs wholesale margin (azure.order_item_margin view)
SELECT o.id
  , c.last_name
  , c.email
  , o.name as order_name
  , o.total
  , SUM(current_quantity) as pieces
  , SUM(original_unit_price)::numeric(12,2) as unit_price
  , SUM(discounted_total)::numeric(12,2) as discounted_total
  , o.total - SUM(retail_dollars)::numeric(12,2) as retail_margin
  , o.total - SUM(wholesale_dollars)::numeric(12,2) as wholesale_margin
  FROM azure.order_items i
  JOIN azure.orders o ON (o.id = i.orders_id)
  JOIN azure.customers c ON (c.id = o.customers_id)
  LEFT JOIN azure.current_prices p ON (p.variants_id = i.variants_id)
  WHERE i.current_quantity > 0
  --AND o.name = '#2553'
  GROUP BY 1,2,3,4,5
  ORDER BY 2,3;


WITH customer_orders AS (
SELECT o.id
  , c.last_name
  , c.email
  , o.name as order_name
  , o.total
  , SUM(current_quantity) as pieces
  , SUM(original_unit_price)::numeric(12,2) as unit_price
  , SUM(discounted_total)::numeric(12,2) as discounted_total
  , o.total - SUM(retail_dollars)::numeric(12,2) as retail_margin
  , o.total - SUM(wholesale_dollars)::numeric(12,2) as wholesale_margin
  FROM azure.order_items i
  JOIN azure.orders o ON (o.id = i.orders_id)
  JOIN azure.customers c ON (c.id = o.customers_id)
  LEFT JOIN azure.current_prices p ON (p.variants_id = i.variants_id)
  WHERE i.current_quantity > 0
  GROUP BY 1,2,3,4,5)
  SELECT last_name
  , email
  , SUM(pieces) as pieces
  , AVG(unit_price)::numeric(12,2) as unit_price_avg
  , SUM(discounted_total) as discounted_total
  , SUM(retail_margin) as retail_margin
  , SUM(wholesale_margin) as wholesale_margin
  FROM customer_orders
  GROUP BY 1,2;
