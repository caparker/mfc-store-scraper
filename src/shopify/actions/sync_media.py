"""Push each variant's primary image to Shopify, recording every step in azure.media.

Per product, for the rows azure.media_sync reports as pending:
1. Rows without a Shopify media id: download, resize, stage, and create the
   media on the product. One media per distinct URL; sizes that share an image
   share the media.
2. Rows with a media id: read the product's media back. A media that is no
   longer on the product is detached locally so step 1 recreates it; the rest
   get their processing status (UPLOADED -> READY or FAILED).
3. Rows whose media is READY: point the variant at it.

Failures are written to the row (`error`, `attempts`); after three attempts,
or when Shopify rejects the image, the view reports the row as failed and it
is left alone until pull-media or a targeted --product-id run.
"""

import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from psycopg import rows, sql

from src.db.postgres import Database
from src.lib.logger import logger
from src.shopify.media_manager import (
    MediaDownloadFailedError,
    StagedUploadFailedError,
    fetch_image,
    fetch_products_media,
    stage_uploads,
    upload_to_target,
)
from src.shopify.mutations import Mutations
from src.shopify.shopify import Shopify, ShopifyQueryError

POLL_INTERVAL_SECONDS = 3
POLL_ATTEMPTS = 10
TERMINAL_STATUSES = ("READY", "FAILED")


class MediaSyncError(Exception):
    """Shopify returned user errors for a media or variant mutation."""


def _fetch_rows(
    database: Database,
    product_id: int | None,
    packaging_code: str | None,
    limit: int | None,
) -> list[dict]:
    base = """
        SELECT
            id
            , packaging_code
            , original_url
            , file_name
            , shopify_media_id
            , shopify_status
            , variant_media_set_at
            , shopify_variant_id
            , product_id
            , shopify_product_id
        FROM azure.media_sync
    """
    # A targeted run retries failed rows too; the default run leaves them alone.
    if packaging_code is not None:
        query = sql.SQL(base + "WHERE sync_state <> 'done' AND packaging_code = %(code)s")
        return database.fetchall(query, {"code": packaging_code}, rows.dict_row)
    if product_id is not None:
        query = sql.SQL(base + "WHERE sync_state <> 'done' AND product_id = %(product_id)s")
        return database.fetchall(query, {"product_id": product_id}, rows.dict_row)
    query = sql.SQL(
        base + "WHERE sync_state = 'pending' ORDER BY product_id, position, id LIMIT %(limit)s"
    )
    return database.fetchall(query, {"limit": limit}, rows.dict_row)


def _product_rows(database: Database, product_id: int) -> list[dict]:
    """Re-read a product's not-done rows after a step has written to them."""
    return database.fetchall(
        sql.SQL(
            """
            SELECT
                id
                , packaging_code
                , original_url
                , file_name
                , shopify_media_id
                , shopify_status
                , variant_media_set_at
                , shopify_variant_id
                , product_id
                , shopify_product_id
            FROM azure.media_sync
            WHERE product_id = %(product_id)s AND sync_state <> 'done'
            """
        ),
        {"product_id": product_id},
        rows.dict_row,
    )


# ---------------------------------------------------------------------- #
# Row bookkeeping
# ---------------------------------------------------------------------- #

def _record_created(database: Database, ids: list[int], media_id: str, status: str) -> None:
    database.execute(
        sql.SQL(
            """
            UPDATE azure.media
            SET
                shopify_media_id = %(media_id)s
                , shopify_status = %(status)s
                , error = NULL
                , attempts = attempts + 1
                , last_attempt_at = now()
                , shopify_updated_at = now()
                , updated_at = now()
            WHERE id = ANY(%(ids)s)
            """
        ),
        {"ids": ids, "media_id": media_id, "status": status},
    )


def _record_status(database: Database, ids: list[int], status: str, error: str | None) -> None:
    database.execute(
        sql.SQL(
            """
            UPDATE azure.media
            SET
                shopify_status = %(status)s
                , error = %(error)s
                , updated_at = now()
            WHERE id = ANY(%(ids)s)
            """
        ),
        {"ids": ids, "status": status, "error": error},
    )


def _record_detached(database: Database, ids: list[int]) -> None:
    database.execute(
        sql.SQL(
            """
            UPDATE azure.media
            SET
                shopify_media_id = NULL
                , shopify_status = NULL
                , variant_media_set_at = NULL
                , updated_at = now()
            WHERE id = ANY(%(ids)s)
            """
        ),
        {"ids": ids},
    )


def _record_failure(
    database: Database, ids: list[int], error: str, permanent: bool = False
) -> None:
    database.execute(
        sql.SQL(
            """
            UPDATE azure.media
            SET
                error = %(error)s
                , shopify_status = CASE
                    WHEN %(permanent)s::boolean THEN 'FAILED' ELSE shopify_status
                END
                , attempts = attempts + 1
                , last_attempt_at = now()
                , updated_at = now()
            WHERE id = ANY(%(ids)s)
            """
        ),
        {"ids": ids, "error": error[:1000], "permanent": permanent},
    )


def _record_variant_set(database: Database, ids: list[int]) -> None:
    database.execute(
        sql.SQL(
            """
            UPDATE azure.media
            SET
                variant_media_set_at = now()
                , error = NULL
                , shopify_updated_at = now()
                , updated_at = now()
            WHERE id = ANY(%(ids)s)
            """
        ),
        {"ids": ids},
    )


# ---------------------------------------------------------------------- #
# Steps
# ---------------------------------------------------------------------- #

def _existing_media_by_url(database: Database, product_id: int) -> dict[str, tuple[str, str]]:
    """{url: (media id, status)} for media already created on this product."""
    found = database.fetchall(
        sql.SQL(
            """
            SELECT DISTINCT ON (m.original_url)
                m.original_url
                , m.shopify_media_id
                , m.shopify_status
            FROM azure.media m
            JOIN azure.packaging pack ON pack.code = m.packaging_code
            WHERE pack.products_id = %(product_id)s
              AND m.shopify_media_id IS NOT NULL
              AND m.shopify_status IN ('UPLOADED', 'PROCESSING', 'READY')
            ORDER BY m.original_url, (m.shopify_status = 'READY') DESC
            """
        ),
        {"product_id": product_id},
    )
    return {url: (media_id, status) for url, media_id, status in found}


def _create_product_media(
    shopify: Shopify, shopify_product_id: str, sources: list[str]
) -> list[dict]:
    resp = shopify.query_file(
        Mutations.product_create_media,
        {
            "productId": shopify_product_id,
            "media": [
                {"originalSource": src, "mediaContentType": "IMAGE"} for src in sources
            ],
        },
    )
    if resp.get("errors"):
        raise ShopifyQueryError(f"{resp['errors']}")
    data = resp["data"]["productCreateMedia"]
    if data["mediaUserErrors"]:
        raise MediaSyncError(f"{data['mediaUserErrors']}")
    created = data["media"]
    if len(created) != len(sources):
        raise MediaSyncError(f"sent {len(sources)} media, Shopify created {len(created)}")
    return created


def _upload_missing(
    shopify: Shopify, database: Database, product_rows: list[dict], counts: dict
) -> None:
    """Create product media for rows that have none, one per distinct URL."""
    to_upload = [r for r in product_rows if not r["shopify_media_id"]]
    if not to_upload:
        return

    product_id = product_rows[0]["product_id"]
    shopify_product_id = product_rows[0]["shopify_product_id"]

    by_url: dict[str, list[dict]] = defaultdict(list)
    for row in to_upload:
        by_url[row["original_url"]].append(row)

    existing = _existing_media_by_url(database, product_id)
    files: list[tuple[str, object]] = []
    for url, url_rows in by_url.items():
        ids = [r["id"] for r in url_rows]
        if url in existing:
            media_id, status = existing[url]
            _record_created(database, ids, media_id, status)
            counts["reused"] += len(ids)
            continue
        try:
            files.append((url, fetch_image(url_rows[0]["file_name"], url)))
        except MediaDownloadFailedError as err:
            _record_failure(database, ids, err.message, permanent=err.permanent)
            counts["failed"] += len(ids)
            logger.warning(f"Product {product_id}: {url}: {err.message}")

    if not files:
        return

    all_ids = [r["id"] for url, _ in files for r in by_url[url]]
    try:
        targets = stage_uploads(shopify, [image for _, image in files])
        sources = [upload_to_target(t, image) for t, (_, image) in zip(targets, files)]
        created = _create_product_media(shopify, shopify_product_id, sources)
    except (StagedUploadFailedError, ShopifyQueryError, MediaSyncError) as err:
        _record_failure(database, all_ids, str(err))
        counts["failed"] += len(all_ids)
        logger.error(f"Product {product_id}: media create failed: {err}")
        return

    for (url, _), node in zip(files, created):
        ids = [r["id"] for r in by_url[url]]
        error = "; ".join(e["message"] for e in node.get("mediaErrors") or []) or None
        _record_created(database, ids, node["id"], node["status"])
        if error:
            _record_status(database, ids, node["status"], error)
        counts["uploaded"] += len(ids)


def _reconcile(shopify: Shopify, database: Database, product_id: int, counts: dict) -> bool:
    """Read the product's media back until every pending media is READY or FAILED.

    Returns True when a media was detached (no longer on the product), in
    which case the caller should run the upload step again.
    """
    detached = False
    for attempt in range(POLL_ATTEMPTS):
        product_rows = [r for r in _product_rows(database, product_id) if r["shopify_media_id"]]
        waiting = [r for r in product_rows if r["shopify_status"] not in TERMINAL_STATUSES]
        if not waiting:
            return detached

        shopify_product_id = product_rows[0]["shopify_product_id"]
        node = fetch_products_media(shopify, [shopify_product_id]).get(shopify_product_id)
        if node is None:
            raise MediaSyncError(f"{shopify_product_id} no longer exists in Shopify")

        on_product = {m["id"]: m for m in node["media"]["nodes"]}
        complete = not node["media"]["pageInfo"]["hasNextPage"]

        by_media: dict[str, list[dict]] = defaultdict(list)
        for row in waiting:
            by_media[row["shopify_media_id"]].append(row)

        still_waiting = False
        for media_id, media_rows in by_media.items():
            ids = [r["id"] for r in media_rows]
            media = on_product.get(media_id)
            if media is None:
                if complete:
                    _record_detached(database, ids)
                    counts["detached"] += len(ids)
                    detached = True
                continue
            status = media["status"]
            if status in TERMINAL_STATUSES:
                error = "; ".join(e["message"] for e in media.get("mediaErrors") or []) or None
                _record_status(database, ids, status, error)
                counts["ready" if status == "READY" else "failed"] += len(ids)
            else:
                still_waiting = True

        if not still_waiting:
            return detached
        if attempt < POLL_ATTEMPTS - 1:
            time.sleep(POLL_INTERVAL_SECONDS)

    logger.warning(f"Product {product_id}: media still processing; will check next run")
    return detached


def _set_variant_media(shopify: Shopify, database: Database, product_id: int, counts: dict) -> None:
    ready = [
        r for r in _product_rows(database, product_id)
        if r["shopify_status"] == "READY" and r["variant_media_set_at"] is None
    ]
    if not ready:
        return

    ids = [r["id"] for r in ready]
    resp = shopify.query_file(
        Mutations.product_variants_bulk_update,
        {
            "productId": ready[0]["shopify_product_id"],
            "variants": [
                {"id": r["shopify_variant_id"], "mediaId": r["shopify_media_id"]} for r in ready
            ],
            "namespace": "internal",
            "key": "id",
        },
    )
    user_errors = (resp.get("data", {}).get("productVariantsBulkUpdate") or {}).get("userErrors")
    errors = resp.get("errors") or user_errors
    if errors:
        _record_failure(database, ids, f"variant update: {errors}")
        counts["failed"] += len(ids)
        logger.error(f"Product {product_id}: variant image update failed: {errors}")
        return

    _record_variant_set(database, ids)
    counts["variants"] += len(ids)


def _sync_product(shopify: Shopify, database: Database, product_rows: list[dict]) -> dict:
    counts = defaultdict(int)
    product_id = product_rows[0]["product_id"]

    _upload_missing(shopify, database, product_rows, counts)
    if _reconcile(shopify, database, product_id, counts):
        # Something was detached; recreate it once, then read back again.
        _upload_missing(shopify, database, _product_rows(database, product_id), counts)
        _reconcile(shopify, database, product_id, counts)
    _set_variant_media(shopify, database, product_id, counts)
    return counts


# ---------------------------------------------------------------------- #
# Entry point
# ---------------------------------------------------------------------- #

def sync_media(
    product_id: int | None = None,
    packaging_code: str | None = None,
    max_workers: int = 3,
    limit: int | None = None,
) -> dict[str, int]:
    """Create each pending primary image on its Shopify product and set it on the variant."""
    logger.info("Starting Shopify media sync")

    database = Database()
    pending = _fetch_rows(database, product_id, packaging_code, limit)
    logger.info(f"Found {len(pending)} image(s) to sync")
    totals: dict[str, int] = defaultdict(int)
    if not pending:
        return dict(totals)

    grouped: dict[int, list[dict]] = defaultdict(list)
    for row in pending:
        grouped[row["product_id"]].append(row)
    logger.info(f"Grouped into {len(grouped)} product(s)")

    shopify = Shopify()
    shopify.get_token()  # Prime token so worker threads don't race on auth.

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_sync_product, shopify, database, product_rows): pid
            for pid, product_rows in grouped.items()
        }
        for fut in as_completed(futures):
            pid = futures[fut]
            try:
                for key, value in fut.result().items():
                    totals[key] += value
            except Exception as err:  # noqa: BLE001
                # Anything not caught per row: leave the rows as they are.
                logger.error(f"Product {pid}: media sync failed: {err}")
                totals["errored_products"] += 1

    logger.success(
        f"Media sync complete: {totals['uploaded']} uploaded, {totals['reused']} reused, "
        f"{totals['ready']} ready, {totals['variants']} variant image(s) set, "
        f"{totals['detached']} detached, {totals['failed']} failed"
    )
    return dict(totals)
