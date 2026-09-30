-- Product + variant + current price (azure.product_search view)
SELECT * FROM azure.product_search
ORDER BY products_id, packaging_code
