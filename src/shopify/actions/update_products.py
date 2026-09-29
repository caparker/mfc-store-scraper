"""Action for pushing dirty azure.products rows to Shopify."""

from concurrent.futures import ThreadPoolExecutor, as_completed

from psycopg import sql, rows

from src.azure.azure_shopify_category_map import AZURE_SHOPIFY_CATEGORY_MAP
from src.db.models.product import ProductModel
from src.db.postgres import Database
from src.lib.logger import logger
from src.shopify.mutations import Mutations
from src.shopify.shopify import Shopify


class ProductUpdateError(Exception):
    """Raised when Shopify returns userErrors for a product update."""


def _build_product_input(product: ProductModel) -> dict:
    _, secondary_category = product.category.split(".")
    shopify_category = AZURE_SHOPIFY_CATEGORY_MAP.get(secondary_category)

    if shopify_category is None:
        logger.warning(
            f"No Shopify category mapping for '{secondary_category}' "
            f"(product {product.id} {product.name}); skipping category field"
        )

    return {
        "id": product.shopify_product_id,
        "handle": product.slug,
        "title": product.name,
        "descriptionHtml": product.description,
        "productType": secondary_category,
        "category": shopify_category,
        "tags": [
            product.category.split(".")[0],
            secondary_category,
            f"storage_{product.storage_climate}",
        ],
        "vendor": "Azure Standard",
        "status": product.shopify_status.value,
    }


def _update_product(shopify: Shopify, product: ProductModel) -> None:
    resp = shopify.query_file(
        Mutations.product_update,
        {"product": _build_product_input(product)},
    )

    data = resp.get("data", {}).get("productUpdate", {}) or {}
    user_errors = data.get("userErrors", []) or []
    top_errors = resp.get("errors", []) or []

    if top_errors or user_errors:
        raise ProductUpdateError(f"{top_errors or user_errors}")


def update_products(
    product_id: int | None = None,
    max_workers: int = 5,
    limit: int | None = None,
):
    """Push product-level updates to Shopify for any dirty rows.

    Dirty = shopify_product_id IS NOT NULL AND
            (shopify_updated_at IS NULL OR shopify_updated_at < updated_at).
    Rows with shopify_status = DELETED are never pushed.
    """
    logger.info("Starting Shopify product update")

    database = Database()

    if product_id is not None:
        query = sql.SQL(
            """
            SELECT *
            FROM azure.products
            WHERE id = %(id)s
              AND shopify_product_id IS NOT NULL
              AND shopify_status <> 'DELETED'
            LIMIT 1
            """
        )
        products = database.fetchall(
            query, {"id": product_id}, rows.class_row(ProductModel)
        )
    else:
        query = sql.SQL(
            """
            SELECT *
            FROM azure.products
            WHERE shopify_product_id IS NOT NULL
              AND shopify_status <> 'DELETED'
              AND (shopify_updated_at IS NULL OR shopify_updated_at < updated_at)
            LIMIT %(limit)s
            """
        )
        products = database.fetchall(query, { "limit": limit }, rows.class_row(ProductModel))

    logger.info(f"Found {len(products)} product(s) to update")

    if not products:
        return

    shopify = Shopify()
    shopify.get_token()  # Prime token so worker threads don't race on auth.

    updated = 0
    failed = 0

    def _task(product: ProductModel):
        _update_product(shopify, product)
        return product

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_task, p): p for p in products}
        for fut in as_completed(futures):
            product = futures[fut]
            try:
                fut.result()
                database.batch_execute(
                    sql.SQL(
                        """
                        UPDATE azure.products
                        SET shopify_updated_at = now()
                        WHERE id = %(id)s
                        """
                    ),
                    [{"id": product.id}],
                )
                logger.debug(f"Updated {product.shopify_product_id} ({product.name})")
                updated += 1
            except Exception as e:  # noqa: BLE001
                logger.error(f"Failed to update {product.name}: {e}")
                failed += 1

    logger.success(f"Product update complete: {updated} updated, {failed} failed")
