-- Per line item retail vs wholesale margin (azure.order_item_margin view)
SELECT * FROM azure.order_item_margin
ORDER BY ordered_at DESC
