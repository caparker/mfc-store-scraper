"""Action for reconciling azure.products.shopify_status with Shopify."""

from psycopg import sql

from src.db.postgres import Database
from src.lib.logger import logger
from src.shopify.queries import Queries
from src.shopify.shopify import Shopify, ShopifyQueryError
from src.shopify.types.models.product import ProductStatus

VENDOR = "Azure Standard"
PAGE_SIZE = 250


def _check_errors(resp: dict) -> None:
    errors = resp.get("errors")
    if errors:
        raise ShopifyQueryError(f"{errors}")


def _fetch_vendor_products(shopify: Shopify) -> dict[str, dict]:
    """Return {shopify_product_id: node} for every product with our vendor."""
    products: dict[str, dict] = {}
    cursor = None

    while True:
        resp = shopify.query_file(
            Queries.products_status_page,
            {"first": PAGE_SIZE, "after": cursor, "query": f"vendor:'{VENDOR}'"},
        )
        _check_errors(resp)

        connection = resp["data"]["products"]
        for node in connection["nodes"]:
            products[node["id"]] = node

        page_info = connection["pageInfo"]
        if not page_info["hasNextPage"]:
            return products
        cursor = page_info["endCursor"]


def _fetch_by_ids(shopify: Shopify, ids: list[str]) -> dict[str, dict | None]:
    """Return {shopify_product_id: node or None} for the given ids.

    None means Shopify has no product with that id, i.e. it was deleted.
    """
    found: dict[str, dict | None] = {}

    for start in range(0, len(ids), PAGE_SIZE):
        chunk = ids[start:start + PAGE_SIZE]
        resp = shopify.query_file(Queries.products_by_ids, {"ids": chunk})
        _check_errors(resp)
        # nodes(ids:) returns results in input order, null for missing ids.
        for gid, node in zip(chunk, resp["data"]["nodes"]):
            found[gid] = node or None

    return found


def pull_product_status(dry_run: bool = False) -> dict[str, int]:
    """Make azure.products.shopify_status match Shopify.

    - Status differences are written locally. shopify_updated_at is advanced
      for rows that were clean so the pull does not make them dirty; rows that
      were already dirty stay dirty.
    - Rows whose Shopify product no longer exists are set to DELETED.
    - Shopify products with our vendor but no DB row are reported as orphans.

    Shopify wins: any local status change not yet pushed is overwritten.
    """
    logger.info("Pulling product status from Shopify")

    database = Database()
    shopify = Shopify()

    db_rows = database.fetchall(
        sql.SQL(
            """
            SELECT
                id
                , shopify_product_id
                , shopify_status
            FROM azure.products
            WHERE shopify_product_id IS NOT NULL
            """
        ),
        {},
    )
    local = {gid: (pid, status) for pid, gid, status in db_rows}
    logger.info(f"{len(local)} product(s) in DB with a Shopify id")

    remote = _fetch_vendor_products(shopify)
    logger.info(f"{len(remote)} product(s) in Shopify with vendor '{VENDOR}'")

    orphans = [node for gid, node in remote.items() if gid not in local]

    # Anything in the DB but not in the vendor listing may have been deleted,
    # or may simply have a different vendor now. Check by id before deciding.
    missing = [gid for gid in local if gid not in remote]
    if missing:
        for gid, node in _fetch_by_ids(shopify, missing).items():
            if node is not None:
                logger.warning(
                    f"{gid} exists in Shopify with vendor '{node.get('vendor')}' "
                    f"(expected '{VENDOR}')"
                )
                remote[gid] = node

    changes: list[dict] = []
    deleted = 0
    for gid, (pid, local_status) in local.items():
        node = remote.get(gid)
        remote_status = node["status"] if node else ProductStatus.DELETED.value
        if remote_status == local_status:
            continue
        if node is None:
            deleted += 1
            logger.warning(f"Product {pid} ({gid}) no longer exists in Shopify")
        else:
            logger.debug(f"Product {pid}: {local_status} -> {remote_status}")
        changes.append({"id": pid, "status": remote_status})

    for node in orphans:
        logger.warning(
            f"Orphan in Shopify: {node['id']} [{node['status']}] "
            f"{node['handle']} ({node['title']})"
        )

    counts = {
        "changed": len(changes) - deleted,
        "deleted": deleted,
        "orphans": len(orphans),
    }

    if dry_run:
        logger.info(f"Dry run; not writing {len(changes)} change(s)")
        return counts

    if changes:
        database.batch_execute(
            sql.SQL(
                """
                UPDATE azure.products
                SET
                    shopify_status = %(status)s
                    , shopify_updated_at = CASE
                        WHEN shopify_updated_at >= updated_at THEN now()
                        ELSE shopify_updated_at
                    END
                WHERE id = %(id)s
                """
            ),
            changes,
        )

    logger.success(
        f"Status pull complete: {counts['changed']} status change(s), "
        f"{counts['deleted']} deleted, {counts['orphans']} orphan(s)"
    )
    return counts
