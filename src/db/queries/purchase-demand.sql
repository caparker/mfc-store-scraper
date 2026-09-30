-- Outstanding units per order line (azure.purchase_demand view)
SELECT * FROM azure.purchase_demand
ORDER BY order_name, packaging_code
