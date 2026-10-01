-- Most recent price change per packaging code that our customers order (azure.price_latest_change view)
SELECT p.name
  , pr.current_retail_dollars
  , pr.retail_change::numeric(12,2)
  , pr.current_wholesale_dollars
  , pr.wholesale_change::numeric(12,2)
FROM azure.price_latest_change pr
  JOIN azure.packaging v ON (pr.shopify_variant_id = v.shopify_variant_id)
  JOIN azure.products p ON (v.products_id = p.id)
WHERE pr.shopify_variant_id IN (SELECT shopify_variant_id FROM azure.order_items)
ORDER BY retail_change DESC
