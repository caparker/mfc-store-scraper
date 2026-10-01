"""Reconcile azure.media with the media actually on each Shopify product.

Shopify keeps the uploaded file name in the CDN url, and Azure file names are
unique, so a product's media can be matched back to azure.media rows by name.
For every product with a Shopify id:
- a row whose file name matches a media on the product gets that media's id
  and status;
- a row whose media id is no longer on the product is cleared so sync-media
  recreates it;
- the primary row of each variant is marked as set when the variant's image is
  that media, and cleared otherwise.

Media on the product that match no row are counted and left alone.
"""

from collections import defaultdict

from psycopg import rows, sql

from src.db.postgres import Database
from src.lib.logger import logger
from src.shopify.media_manager import fetch_products_media, match_media_by_name
from src.shopify.shopify import Shopify


def _fetch_rows(database: Database, product_id: int | None, limit: int | None) -> list[dict]:
    products = database.fetchall(
        sql.SQL(
            """
            SELECT id
            FROM azure.products
            WHERE shopify_product_id IS NOT NULL
              AND shopify_status <> 'DELETED'
              AND (%(product_id)s::integer IS NULL OR id = %(product_id)s)
            ORDER BY id
            LIMIT %(limit)s
            """
        ),
        {"product_id": product_id, "limit": limit},
    )
    return database.fetchall(
        sql.SQL(
            """
            SELECT
                m.id
                , m.file_name
                , m.shopify_media_id
                , m.shopify_status
                , m.variant_media_set_at
                , (m.id = pm.id) AS is_primary
                , pack.shopify_variant_id
                , prod.id AS product_id
                , prod.shopify_product_id
            FROM azure.media m
            JOIN azure.packaging pack ON pack.code = m.packaging_code
            JOIN azure.products prod ON prod.id = pack.products_id
            JOIN azure.primary_media pm ON pm.packaging_code = m.packaging_code
            WHERE prod.id = ANY(%(ids)s)
            ORDER BY prod.id, m.id
            """
        ),
        {"ids": [p[0] for p in products]},
        rows.dict_row,
    )


def pull_media(
    product_id: int | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Make azure.media match the media on each Shopify product. Shopify wins."""
    logger.info("Pulling product media from Shopify")

    database = Database()
    shopify = Shopify()

    db_rows = _fetch_rows(database, product_id, limit)
    by_product: dict[str, list[dict]] = defaultdict(list)
    for row in db_rows:
        by_product[row["shopify_product_id"]].append(row)
    logger.info(f"{len(db_rows)} media row(s) across {len(by_product)} product(s)")

    remote = fetch_products_media(shopify, list(by_product))

    counts = {
        "products": len(by_product),
        "matched": 0,
        "detached": 0,
        "variant_set": 0,
        "variant_cleared": 0,
        "unmatched_in_shopify": 0,
        "missing_products": 0,
        "incomplete_products": 0,
    }
    changes: list[dict] = []

    for gid, product_rows in by_product.items():
        node = remote.get(gid)
        if node is None:
            counts["missing_products"] += 1
            logger.warning(f"Product {product_rows[0]['product_id']} ({gid}) not in Shopify")
            continue

        media_nodes = node["media"]["nodes"]
        on_product = {m["id"]: m for m in media_nodes}
        complete = not node["media"]["pageInfo"]["hasNextPage"]
        if not complete:
            counts["incomplete_products"] += 1
        variant_image = {
            v["id"]: (v["media"]["nodes"][0]["id"] if v["media"]["nodes"] else None)
            for v in node["variants"]["nodes"]
        }

        matched_ids: set[str] = set()
        for row in product_rows:
            media = match_media_by_name(media_nodes, row["file_name"])
            if media is None and row["shopify_media_id"] in on_product:
                media = on_product[row["shopify_media_id"]]
            if media is None and not complete and row["shopify_media_id"]:
                continue  # cannot tell; media list was cut off

            media_id = media["id"] if media else None
            status = media["status"] if media else None
            if media_id:
                matched_ids.add(media_id)
            variant_set = bool(
                row["is_primary"] and media_id
                and variant_image.get(row["shopify_variant_id"]) == media_id
            )

            if media_id and not row["shopify_media_id"]:
                counts["matched"] += 1
            elif row["shopify_media_id"] and not media_id:
                counts["detached"] += 1
            if variant_set and row["variant_media_set_at"] is None:
                counts["variant_set"] += 1
            elif not variant_set and row["variant_media_set_at"] is not None:
                counts["variant_cleared"] += 1

            unchanged = (
                media_id == row["shopify_media_id"]
                and status == row["shopify_status"]
                and variant_set == (row["variant_media_set_at"] is not None)
            )
            if not unchanged:
                changes.append(
                    {
                        "id": row["id"],
                        "media_id": media_id,
                        "status": status,
                        "variant_set": variant_set,
                    }
                )

        counts["unmatched_in_shopify"] += len(set(on_product) - matched_ids)

    if dry_run:
        logger.info(f"Dry run; not writing {len(changes)} change(s)")
        return counts

    if changes:
        database.batch_execute(
            sql.SQL(
                """
                UPDATE azure.media
                SET
                    shopify_media_id = %(media_id)s::text
                    , shopify_status = %(status)s::text
                    , variant_media_set_at = CASE
                        WHEN %(variant_set)s::boolean THEN COALESCE(variant_media_set_at, now())
                        ELSE NULL
                    END
                    , error = CASE WHEN %(media_id)s::text IS NULL THEN error ELSE NULL END
                    , attempts = CASE WHEN %(media_id)s::text IS NULL THEN attempts ELSE 0 END
                    , shopify_updated_at = CASE
                        WHEN %(media_id)s::text IS NULL THEN shopify_updated_at ELSE now()
                    END
                    , updated_at = now()
                WHERE id = %(id)s
                """
            ),
            changes,
        )

    logger.success(
        f"Media pull complete: {counts['matched']} matched, {counts['detached']} detached, "
        f"{counts['variant_set']} variant image(s) confirmed, "
        f"{counts['variant_cleared']} cleared, "
        f"{counts['unmatched_in_shopify']} Shopify media with no local row"
    )
    return counts
