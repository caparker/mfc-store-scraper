-- Customers with membership and sync state
SELECT
    id
    , email
    , first_name
    , last_name
    , phone
    , member_number
    , membership_status
    , member_since
    , membership_expires
    , notes
    , shopify_customer_id
    , last_changed_fields
    , shopify_updated_at
    , updated_at
FROM azure.customers
ORDER BY id
