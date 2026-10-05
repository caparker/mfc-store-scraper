-- Total price change per variant that our customers order (azure.price_total_change view)
SELECT p.name
  , pr.current_retail_dollars
  , pr.retail_change::numeric(12,2)
  , pr.current_wholesale_dollars
  , pr.wholesale_change::numeric(12,2)
FROM azure.price_total_change pr
  JOIN azure.variants v ON (v.id = pr.variants_id)
  JOIN azure.products p ON (p.id = v.products_id)
WHERE pr.variants_id IN (SELECT variants_id FROM azure.order_items WHERE variants_id IS NOT NULL)
ORDER BY retail_change DESC
