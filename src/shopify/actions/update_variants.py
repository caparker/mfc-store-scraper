"""Action for pushing dirty azure.packaging rows to Shopify as variant updates."""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List

from psycopg import sql

from src.db.postgres import Database, MARKUP_PERCENTAGE
from src.lib.logger import logger
from src.shopify.mutations import Mutations
from src.shopify.shopify import Shopify


class VariantUpdateError(Exception):
    """Raised when Shopify returns userErrors for a variant update."""


# Row shape returned by the dirty query.
# (packaging_id, packaging_code, shopify_variant_id, shopify_product_id, retail_dollars)
DirtyRow = tuple

INVENTORY_POLICY = "DENY"


def _fetch_dirty_rows(
    database: Database,
    packaging_code: str | None,
    product_id: int | None,
    limit: int | None = None,
) -> List[DirtyRow]:
    base = """
        WITH latest_price AS (
            SELECT DISTINCT ON (packaging_code)
                packaging_code, retail_dollars, created_at
            FROM azure.prices
            ORDER BY packaging_code, created_at DESC
        )
        SELECT
            pack.id,
            pack.code,
            pack.shopify_variant_id,
            prod.shopify_product_id,
            lp.retail_dollars as retail_dollars
        FROM azure.packaging pack
        JOIN azure.products prod ON prod.id = pack.products_id
        LEFT JOIN latest_price lp ON lp.packaging_code = pack.code
        WHERE pack.shopify_variant_id IS NOT NULL
          AND prod.shopify_product_id IS NOT NULL
          AND prod.shopify_status <> 'DELETED'
          AND lp.retail_dollars IS NOT NULL
    """

    if packaging_code is not None:
        query = sql.SQL(base + " AND pack.code = %(code)s")
        return database.fetchall(query, {"code": packaging_code})

    if product_id is not None:
        query = sql.SQL(base + " AND prod.id = %(product_id)s")
        return database.fetchall(query, {"product_id": product_id})

    query = sql.SQL(
        base
        + """
          AND (
            pack.shopify_updated_at IS NULL
            OR pack.shopify_updated_at < GREATEST(pack.updated_at, lp.created_at)
          ) LIMIT %(limit)s
        """
    )
    return database.fetchall(query, { "limit": limit })

def _update_product_variants(
    shopify: Shopify,
    shopify_product_id: str,
    variants_payload: list[dict],
) -> None:
    resp = shopify.query_file(
        Mutations.product_variants_bulk_update,
        {
            "productId": shopify_product_id,
            "variants": variants_payload,
            "namespace": "internal",
            "key": "id",
        },
    )

    data = resp.get("data", {}).get("productVariantsBulkUpdate", {}) or {}
    user_errors = data.get("userErrors", []) or []
    top_errors = resp.get("errors", []) or []

    if top_errors or user_errors:
        raise VariantUpdateError(f"{top_errors or user_errors}")

def update_variants(
    packaging_code: str | None = None,
    product_id: int | None = None,
    max_workers: int = 5,
    limit: int | None = None,
):
    """Push variant-level updates (price, cost, inventory policy) to Shopify
    for any dirty packaging rows. Stock is handled separately by update_stock.

    Dirty = shopify_variant_id IS NOT NULL AND
            shopify_updated_at < GREATEST(packaging.updated_at, latest_price.created_at).
    """
    logger.info("Starting Shopify variant update")

    database = Database()
    rows = _fetch_dirty_rows(database, packaging_code, product_id, limit)
    logger.info(f"Found {len(rows)} variant(s) to update")
    if not rows:
        return

    shopify = Shopify()
    shopify.get_token()  # Prime token so worker threads don't race on auth.

    # Group by shopify_product_id so we do one bulk mutation per product.
    grouped: dict[str, list[DirtyRow]] = defaultdict(list)
    for row in rows:
        grouped[row[3]].append(row)

    logger.info(f"Grouped into {len(grouped)} product batch(es)")

    updated = 0
    failed = 0


    def _task(shopify_product_id: str, product_rows: list[DirtyRow]):
        ## r[4] is the retail price
        variants_payload = [
            {
                "id": r[2],
                "price": f"{round(float(r[4]) / (1 - (MARKUP_PERCENTAGE/100)), 2):.2f}",
                "inventoryPolicy": INVENTORY_POLICY,
                "inventoryItem": { "cost": f"{round(float(r[4]), 2):.2f}" }
            }
            for r in product_rows
        ]
        _update_product_variants(shopify, shopify_product_id, variants_payload)
        return product_rows

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_task, spid, product_rows): (spid, product_rows)
            for spid, product_rows in grouped.items()
        }
        for fut in as_completed(futures):
            spid, product_rows = futures[fut]
            try:
                fut.result()
                database.batch_execute(
                    sql.SQL(
                        """
                        UPDATE azure.packaging
                        SET shopify_updated_at = now()
                        WHERE id = %(id)s
                        """
                    ),
                    [{"id": r[0]} for r in product_rows],
                )
                logger.debug(
                    f"Updated {len(product_rows)} variant(s) for product {spid}"
                )
                updated += len(product_rows)
            except Exception as e:  # noqa: BLE001
                logger.error(f"Failed to update product {spid}: {e}")
                failed += len(product_rows)

    logger.success(f"Variant update complete: {updated} updated, {failed} failed")
