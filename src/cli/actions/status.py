"""Summarize what would sync to Shopify."""

from psycopg import sql

from src.db.postgres import Database


def sync_status() -> dict[str, int]:
    db = Database()

    counts_query = sql.SQL("""
        WITH latest_price AS (
            SELECT DISTINCT ON (variants_id)
                variants_id, created_at
            FROM azure.prices
            ORDER BY variants_id, created_at DESC
        )
        SELECT
            (SELECT count(*) FROM azure.products
                WHERE shopify_product_id IS NULL) AS products_new,

            (SELECT count(*) FROM azure.products
                WHERE shopify_product_id IS NOT NULL
                  AND shopify_status <> 'DELETED'
                  AND (shopify_updated_at IS NULL
                       OR shopify_updated_at < updated_at)) AS products_dirty,

            (SELECT count(*) FROM azure.products
                WHERE shopify_status = 'DELETED') AS products_deleted,

            (SELECT count(*) FROM azure.variants v
                JOIN azure.products prod ON prod.id = v.products_id
                WHERE prod.shopify_product_id IS NOT NULL
                  AND v.shopify_variant_id IS NULL) AS variants_new,

            (SELECT count(*) FROM azure.variants v
                JOIN azure.products prod ON prod.id = v.products_id
                LEFT JOIN latest_price lp ON lp.variants_id = v.id
                WHERE v.shopify_variant_id IS NOT NULL
                  AND prod.shopify_product_id IS NOT NULL
                  AND prod.shopify_status <> 'DELETED'
                  AND (
                    v.shopify_updated_at IS NULL
                    OR v.shopify_updated_at < GREATEST(
                         v.updated_at,
                         COALESCE(lp.created_at, 'epoch'::timestamptz)
                       )
                  )) AS variants_dirty,

            (SELECT count(*) FROM azure.variants v
                JOIN azure.products prod ON prod.id = v.products_id
                WHERE v.shopify_variant_id IS NOT NULL
                  AND prod.shopify_product_id IS NOT NULL
                  AND prod.shopify_status <> 'DELETED'
                  AND v.stock IS DISTINCT FROM v.shopify_stock) AS stock_dirty,

            (SELECT count(*) FROM azure.media_sync
                WHERE sync_state = 'pending') AS media_pending,

            (SELECT count(*) FROM azure.media_sync
                WHERE sync_state = 'failed') AS media_failed,

            (SELECT count(*) FROM azure.customers
                WHERE shopify_customer_id IS NULL) AS customers_new,

            (SELECT count(*) FROM azure.customers
                WHERE shopify_customer_id IS NOT NULL
                  AND (shopify_updated_at IS NULL
                       OR shopify_updated_at < updated_at)) AS customers_dirty,

            (SELECT count(*) FROM azure.open_orders) AS orders_open,

            (SELECT count(*) FROM azure.purchase_demand
                WHERE outstanding > 0) AS purchase_items,

            (SELECT COALESCE(sum(outstanding), 0) FROM azure.purchase_demand) AS purchase_units
    """)

    row = db.fetchone(counts_query, {})
    return {
        "products_new": row[0],
        "products_dirty": row[1],
        "products_deleted": row[2],
        "variants_new": row[3],
        "variants_dirty": row[4],
        "stock_dirty": row[5],
        "media_pending": row[6],
        "media_failed": row[7],
        "customers_new": row[8],
        "customers_dirty": row[9],
        "orders_open": row[10],
        "purchase_items": row[11],
        "purchase_units": int(row[12]),
    }


def sync_samples(limit: int = 10) -> dict[str, list[tuple]]:
    """Return a small sample of dirty rows in each category for diagnostics."""
    db = Database()

    products_new = db.fetchall(
        sql.SQL("""
            SELECT id, name
            FROM azure.products
            WHERE shopify_product_id IS NULL
            ORDER BY created_at DESC
            LIMIT %(limit)s
        """),
        {"limit": limit},
    )

    products_dirty = db.fetchall(
        sql.SQL("""
            SELECT id, name, last_changed_fields
            FROM azure.products
            WHERE shopify_product_id IS NOT NULL
              AND shopify_status <> 'DELETED'
              AND (shopify_updated_at IS NULL OR shopify_updated_at < updated_at)
            ORDER BY updated_at DESC
            LIMIT %(limit)s
        """),
        {"limit": limit},
    )

    variants_new = db.fetchall(
        sql.SQL("""
            SELECT v.id, v.code, prod.name
            FROM azure.variants v
            JOIN azure.products prod ON prod.id = v.products_id
            WHERE prod.shopify_product_id IS NOT NULL
              AND v.shopify_variant_id IS NULL
            ORDER BY prod.id, v.code
            LIMIT %(limit)s
        """),
        {"limit": limit},
    )

    variants_dirty = db.fetchall(
        sql.SQL("""
            WITH latest_price AS (
                SELECT DISTINCT ON (variants_id)
                    variants_id, created_at
                FROM azure.prices
                ORDER BY variants_id, created_at DESC
            )
            SELECT v.id, v.code, prod.name, v.last_changed_fields
            FROM azure.variants v
            JOIN azure.products prod ON prod.id = v.products_id
            LEFT JOIN latest_price lp ON lp.variants_id = v.id
            WHERE v.shopify_variant_id IS NOT NULL
              AND prod.shopify_product_id IS NOT NULL
              AND prod.shopify_status <> 'DELETED'
              AND (
                v.shopify_updated_at IS NULL
                OR v.shopify_updated_at < GREATEST(
                     v.updated_at,
                     COALESCE(lp.created_at, 'epoch'::timestamptz)
                   )
              )
            ORDER BY GREATEST(v.updated_at, COALESCE(lp.created_at, 'epoch'::timestamptz)) DESC
            LIMIT %(limit)s
        """),
        {"limit": limit},
    )

    stock_dirty = db.fetchall(
        sql.SQL("""
            SELECT v.id, v.code, prod.name, v.stock, v.shopify_stock
            FROM azure.variants v
            JOIN azure.products prod ON prod.id = v.products_id
            WHERE v.shopify_variant_id IS NOT NULL
              AND prod.shopify_product_id IS NOT NULL
              AND prod.shopify_status <> 'DELETED'
              AND v.stock IS DISTINCT FROM v.shopify_stock
            ORDER BY prod.id, v.code
            LIMIT %(limit)s
        """),
        {"limit": limit},
    )

    media_failed = db.fetchall(
        sql.SQL("""
            SELECT id, variant_code, product_name, attempts, error
            FROM azure.media_sync
            WHERE sync_state = 'failed'
            ORDER BY last_attempt_at DESC NULLS LAST
            LIMIT %(limit)s
        """),
        {"limit": limit},
    )

    customers_new = db.fetchall(
        sql.SQL("""
            SELECT id, email, member_number
            FROM azure.customers
            WHERE shopify_customer_id IS NULL
            ORDER BY created_at DESC
            LIMIT %(limit)s
        """),
        {"limit": limit},
    )

    customers_dirty = db.fetchall(
        sql.SQL("""
            SELECT id, email, member_number, last_changed_fields
            FROM azure.customers
            WHERE shopify_customer_id IS NOT NULL
              AND (shopify_updated_at IS NULL OR shopify_updated_at < updated_at)
            ORDER BY updated_at DESC
            LIMIT %(limit)s
        """),
        {"limit": limit},
    )

    return {
        "products_new": products_new,
        "products_dirty": products_dirty,
        "variants_new": variants_new,
        "variants_dirty": variants_dirty,
        "stock_dirty": stock_dirty,
        "media_failed": media_failed,
        "customers_new": customers_new,
        "customers_dirty": customers_dirty,
    }
