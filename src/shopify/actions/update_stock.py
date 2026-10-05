"""Action for pushing stock to Shopify in cross-product batches."""

from concurrent.futures import ThreadPoolExecutor, as_completed

from psycopg import sql

from src.db.postgres import Database
from src.lib.logger import logger
from src.shopify.mutations import Mutations
from src.shopify.queries import Queries
from src.shopify.shopify import Shopify


class StockUpdateError(Exception):
    """Raised when Shopify returns userErrors for an inventory set."""


# inventorySetOnHandQuantities accepts at most 250 quantities per call.
BATCH_SIZE = 250

# Row shape returned by the dirty query.
# (variants_id, shopify_variant_id, shopify_inventory_item_id, stock)
StockRow = tuple


def _fetch_dirty_stock(
    database: Database,
    variant_code: str | None,
    product_id: int | None,
    limit: int | None,
) -> list[StockRow]:
    base = """
        SELECT
            v.id
            , v.shopify_variant_id
            , v.shopify_inventory_item_id
            , v.stock
        FROM azure.variants v
        JOIN azure.products prod ON prod.id = v.products_id
        WHERE v.shopify_variant_id IS NOT NULL
          AND prod.shopify_product_id IS NOT NULL
          AND prod.shopify_status <> 'DELETED'
    """

    if variant_code is not None:
        return database.fetchall(sql.SQL(base + " AND v.code = %(code)s"), {"code": variant_code})

    if product_id is not None:
        return database.fetchall(sql.SQL(base + " AND prod.id = %(product_id)s"), {"product_id": product_id})

    query = sql.SQL(
        base
        + """
          AND v.stock IS DISTINCT FROM v.shopify_stock
        LIMIT %(limit)s
        """
    )
    return database.fetchall(query, {"limit": limit})


def get_primary_location_id(shopify: Shopify) -> str:
    resp = shopify.query_file(Queries.location_primary, {})
    return resp["data"]["locations"]["nodes"][0]["id"]


def resolve_inventory_item_ids(
    shopify: Shopify,
    database: Database,
    variant_ids: list[str],
) -> dict[str, str]:
    """Return {shopify_variant_id: shopify_inventory_item_id} for the given
    variants, populating azure.variants for any that were missing it."""
    found: dict[str, str] = {}
    updates = []

    for start in range(0, len(variant_ids), BATCH_SIZE):
        batch = variant_ids[start:start + BATCH_SIZE]
        resp = shopify.query_file(Queries.inventory_items_by_variants, {"ids": batch})
        for node in resp["data"]["nodes"]:
            if not node:
                continue
            found[node["id"]] = node["inventoryItem"]["id"]
            updates.append({"variant_id": node["id"], "item_id": node["inventoryItem"]["id"]})

    if updates:
        database.batch_execute(
            sql.SQL(
                """
                UPDATE azure.variants
                SET shopify_inventory_item_id = %(item_id)s
                WHERE shopify_variant_id = %(variant_id)s
                """
            ),
            updates,
        )

    return found


def set_on_hand(shopify: Shopify, location_id: str, entries: list[dict]) -> None:
    """entries: [{"inventory_item_id": ..., "quantity": ...}, ...], at most BATCH_SIZE."""
    resp = shopify.query_file(
        Mutations.inventory_set_on_hand,
        {
            "input": {
                "reason": "correction",
                "setQuantities": [
                    {
                        "inventoryItemId": e["inventory_item_id"],
                        "locationId": location_id,
                        "quantity": int(e["quantity"]),
                    }
                    for e in entries
                ],
            }
        },
    )
    data = resp.get("data", {}).get("inventorySetOnHandQuantities", {}) or {}
    user_errors = data.get("userErrors", []) or []
    top_errors = resp.get("errors", []) or []
    if top_errors or user_errors:
        raise StockUpdateError(f"{top_errors or user_errors}")


def update_stock(
    variant_code: str | None = None,
    product_id: int | None = None,
    max_workers: int = 3,
    limit: int | None = None,
):
    """Push on-hand stock to Shopify for variants whose stock changed.

    Dirty = stock IS DISTINCT FROM shopify_stock. Rows are sent in batches of
    250 regardless of product, and shopify_stock is set to the pushed value on
    success.
    """
    logger.info("Starting Shopify stock update")

    database = Database()
    rows = _fetch_dirty_stock(database, variant_code, product_id, limit)
    logger.info(f"Found {len(rows)} variant(s) with stock to push")
    if not rows:
        return

    shopify = Shopify()
    shopify.get_token()  # Prime token so worker threads don't race on auth.
    location_id = get_primary_location_id(shopify)

    item_ids = {r[1]: r[2] for r in rows if r[2]}
    missing = [r[1] for r in rows if not r[2]]
    if missing:
        item_ids.update(resolve_inventory_item_ids(shopify, database, missing))

    unresolved = [r for r in rows if r[1] not in item_ids]
    for r in unresolved:
        logger.warning(f"No inventory item for variant {r[1]} (variants.id {r[0]}); skipping")
    rows = [r for r in rows if r[1] in item_ids]

    batches = [rows[i:i + BATCH_SIZE] for i in range(0, len(rows), BATCH_SIZE)]
    logger.info(f"Sending {len(batches)} batch(es)")

    updated = 0
    failed = len(unresolved)

    def _task(batch: list[StockRow]) -> list[StockRow]:
        set_on_hand(
            shopify,
            location_id,
            [{"inventory_item_id": item_ids[r[1]], "quantity": r[3]} for r in batch],
        )
        return batch

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_task, b): b for b in batches}
        for fut in as_completed(futures):
            batch = futures[fut]
            try:
                fut.result()
                database.batch_execute(
                    sql.SQL(
                        """
                        UPDATE azure.variants
                        SET shopify_stock = %(stock)s
                        WHERE id = %(id)s
                        """
                    ),
                    [{"id": r[0], "stock": r[3]} for r in batch],
                )
                updated += len(batch)
            except Exception as e:  # noqa: BLE001
                logger.error(f"Failed to push a batch of {len(batch)} stock value(s): {e}")
                failed += len(batch)

    logger.success(f"Stock update complete: {updated} updated, {failed} failed")
