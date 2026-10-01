-- Most recent price change per packaging code (azure.price_latest_change view)
SELECT *
FROM azure.price_latest_change
ORDER BY changed_at DESC
