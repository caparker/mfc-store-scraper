"""Action for creating new azure.products in Shopify."""

from concurrent.futures import ThreadPoolExecutor, as_completed

from psycopg import sql, rows

from src.db.models.product import ProductModel
from src.db.postgres import Database
from src.lib.logger import logger
from src.shopify.actions import (
    create_product,
    create_variants_for_product,
    ProductCreateError,
    ProductVariantCreateError,
)
from src.shopify.types.models.product import ProductStatus


def _fetch_new_products(
        database: Database, product_id: int | None,
        limit: int | None = None,
) -> list[ProductModel]:
    if product_id is not None:
        query = sql.SQL(
            """
            SELECT *
            FROM azure.products
            WHERE id = %(id)s AND shopify_product_id IS NULL
            LIMIT 1
            """
        )
        return database.fetchall(query, {"id": product_id}, rows.class_row(ProductModel))

    query = sql.SQL(
        "SELECT * FROM azure.products WHERE shopify_product_id IS NULL LIMIT %(limit)s"
    )
    return database.fetchall(query, { "limit": limit }, rows.class_row(ProductModel))


def add_products(
    product_id: int | None = None,
    max_workers: int = 5,
    limit: int | None = None,
    status: ProductStatus = ProductStatus.DRAFT,
):
    """Create new products (and their variants) in Shopify.

    New = shopify_product_id IS NULL.
    """
    logger.info("Starting Shopify product creation")

    database = Database()
    products = _fetch_new_products(database, product_id, limit)

    logger.info(f"Found {len(products)} product(s) to create")

    if not products:
        return

    created = 0
    failed = 0

    def _task(product: ProductModel):
        created_product = create_product(product, status=status)
        create_variants_for_product(created_product)
        return product

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_task, p): p for p in products}
        for fut in as_completed(futures):
            product = futures[fut]
            try:
                fut.result()
                logger.debug(f"Created {product.name}")
                created += 1
            except ProductCreateError as e:
                logger.error(f"Product create failed for {product.name}: {e.message}")
                failed += 1
            except ProductVariantCreateError as e:
                logger.error(
                    f"Variant create failed for {product.name}: {e.message} "
                    f"(product exists in Shopify without variants)"
                )
                failed += 1
            except Exception as e:  # noqa: BLE001
                logger.error(f"Failed to create {product.name}: {e}")
                failed += 1

    logger.success(f"Product create complete: {created} created, {failed} failed")
