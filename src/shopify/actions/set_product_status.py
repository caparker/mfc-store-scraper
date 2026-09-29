"""Action for changing the Shopify status recorded for azure.products rows."""

from psycopg import sql

from src.db.postgres import Database
from src.lib.logger import logger
from src.shopify.types.models.product import ProductStatus

from .update_products import update_products


def set_product_status(
    status: ProductStatus,
    product_id: int | None = None,
    push: bool = True,
    max_workers: int = 5,
) -> int:
    """Set shopify_status on products that already exist in Shopify.

    With product_id, only that row is changed. Otherwise every row whose
    status differs is changed. Changing the column marks the row dirty via
    the products_set_updated_at trigger; when push is True the dirty rows
    are pushed to Shopify immediately via update_products.

    Returns the number of rows changed.
    """
    if status is ProductStatus.DELETED:
        raise ValueError("DELETED is set by pull-status, not by hand")

    database = Database()

    if product_id is not None:
        query = sql.SQL(
            """
            UPDATE azure.products
            SET shopify_status = %(status)s
            WHERE id = %(id)s
              AND shopify_product_id IS NOT NULL
              AND shopify_status NOT IN (%(status)s, 'DELETED')
            RETURNING id
            """
        )
        params = {"status": status.value, "id": product_id}
    else:
        query = sql.SQL(
            """
            UPDATE azure.products
            SET shopify_status = %(status)s
            WHERE shopify_product_id IS NOT NULL
              AND shopify_status NOT IN (%(status)s, 'DELETED')
            RETURNING id
            """
        )
        params = {"status": status.value}

    changed = database.fetchall(query, params)
    logger.info(f"Marked {len(changed)} product(s) as {status.value}")

    if changed and push:
        update_products(product_id=product_id, max_workers=max_workers)

    return len(changed)
