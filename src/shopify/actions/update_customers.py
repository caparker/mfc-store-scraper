"""Action for pushing membership info from azure.customers to Shopify."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

from psycopg import sql, rows

from src.db.postgres import Database
from src.lib.logger import logger
from src.shopify.mutations import Mutations
from src.shopify.queries import Queries
from src.shopify.shopify import Shopify, ShopifyQueryError


class CustomerPushError(Exception):
    """Raised when Shopify returns userErrors for a customer push."""


MEMBERSHIP_NAMESPACE = "membership"
MEMBER_TAG = "member"
ACTIVE_STATUS = "active"

# column -> (metafield key, metafield type)
MEMBERSHIP_FIELDS = {
    "member_number": ("member_number", "single_line_text_field"),
    "membership_status": ("status", "single_line_text_field"),
    "member_since": ("member_since", "date"),
    "membership_expires": ("expires", "date"),
}

CUSTOMER_COLUMNS = """
    id
    , shopify_customer_id
    , email
    , first_name
    , last_name
    , phone
    , member_number
    , membership_status
    , member_since
    , membership_expires
"""


def _raise_on_errors(resp: dict, key: str) -> dict:
    if resp.get("errors"):
        raise CustomerPushError(f"{resp['errors']}")
    data = (resp.get("data") or {}).get(key) or {}
    if data.get("userErrors"):
        raise CustomerPushError(f"{data['userErrors']}")
    return data


def _format(value) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _metafields(row: dict) -> tuple[list[dict], list[str]]:
    """Split membership columns into (metafields to set, keys to delete)."""
    to_set: list[dict] = []
    to_delete: list[str] = []
    for column, (key, mtype) in MEMBERSHIP_FIELDS.items():
        value = row.get(column)
        if value is None or value == "":
            to_delete.append(key)
        else:
            to_set.append({
                "namespace": MEMBERSHIP_NAMESPACE,
                "key": key,
                "type": mtype,
                "value": _format(value),
            })
    return to_set, to_delete


def _is_member(row: dict) -> bool:
    return (row.get("membership_status") or "").lower() == ACTIVE_STATUS


def _fetch_dirty(database: Database, customer_id: int | None, limit: int | None) -> list[dict]:
    where = "c.shopify_customer_id IS NOT NULL"
    if customer_id is not None:
        where += " AND c.id = %(customer_id)s"
    else:
        where += " AND (c.shopify_updated_at IS NULL OR c.shopify_updated_at < c.updated_at)"
    return database.fetchall(
        sql.SQL(
            f"""
            SELECT {CUSTOMER_COLUMNS}
            FROM azure.customers c
            WHERE {where}
            ORDER BY c.updated_at
            LIMIT %(limit)s
            """
        ),
        {"customer_id": customer_id, "limit": limit},
        row_factory=rows.dict_row,
    )


def _fetch_new(database: Database, customer_id: int | None, limit: int | None) -> list[dict]:
    where = "c.shopify_customer_id IS NULL"
    if customer_id is not None:
        where += " AND c.id = %(customer_id)s"
    return database.fetchall(
        sql.SQL(
            f"""
            SELECT {CUSTOMER_COLUMNS}
            FROM azure.customers c
            WHERE {where}
            ORDER BY c.created_at
            LIMIT %(limit)s
            """
        ),
        {"customer_id": customer_id, "limit": limit},
        row_factory=rows.dict_row,
    )


def _mark_pushed(
    database: Database, customer_id: int, shopify_customer_id: str | None = None
) -> None:
    database.execute(
        sql.SQL(
            """
            UPDATE azure.customers
            SET shopify_updated_at = now()
                , shopify_customer_id = COALESCE(%(gid)s, shopify_customer_id)
            WHERE id = %(id)s
            """
        ),
        {"id": customer_id, "gid": shopify_customer_id},
    )


def create_customer(shopify: Shopify, row: dict) -> str:
    """Create a Shopify customer from a local row; returns the new Shopify id."""
    if not row.get("email"):
        raise CustomerPushError("cannot create a Shopify customer without an email")
    to_set, _ = _metafields(row)
    customer_input = {
        "email": row["email"],
        "firstName": row.get("first_name"),
        "lastName": row.get("last_name"),
        "phone": row.get("phone"),
        "tags": [MEMBER_TAG] if _is_member(row) else [],
        "metafields": [
            {"namespace": m["namespace"], "key": m["key"], "type": m["type"], "value": m["value"]}
            for m in to_set
        ],
    }
    customer_input = {k: v for k, v in customer_input.items() if v is not None}
    resp = shopify.query_file(Mutations.customer_create, {"input": customer_input})
    data = _raise_on_errors(resp, "customerCreate")
    return data["customer"]["id"]


def push_membership(shopify: Shopify, row: dict) -> None:
    """Push membership metafields and the member tag for an existing Shopify customer."""
    gid = row["shopify_customer_id"]

    resp = shopify.query_file(Queries.customer_membership, {"id": gid})
    if resp.get("errors"):
        raise CustomerPushError(f"{resp['errors']}")
    remote = (resp.get("data") or {}).get("customer")
    if remote is None:
        raise CustomerPushError(f"{gid} no longer exists in Shopify")
    remote_keys = {m["key"] for m in remote["metafields"]["nodes"]}
    remote_tags = set(remote.get("tags") or [])

    to_set, to_delete = _metafields(row)
    if to_set:
        _raise_on_errors(
            shopify.query_file(
                Mutations.metafields_set,
                {"metafields": [{"ownerId": gid, **m} for m in to_set]},
            ),
            "metafieldsSet",
        )
    to_delete = [k for k in to_delete if k in remote_keys]
    if to_delete:
        _raise_on_errors(
            shopify.query_file(
                Mutations.metafields_delete,
                {
                    "metafields": [
                        {"ownerId": gid, "namespace": MEMBERSHIP_NAMESPACE, "key": k}
                        for k in to_delete
                    ]
                },
            ),
            "metafieldsDelete",
        )

    is_member = _is_member(row)
    if is_member and MEMBER_TAG not in remote_tags:
        _raise_on_errors(
            shopify.query_file(Mutations.tags_add, {"id": gid, "tags": [MEMBER_TAG]}),
            "tagsAdd",
        )
    elif not is_member and MEMBER_TAG in remote_tags:
        _raise_on_errors(
            shopify.query_file(Mutations.tags_remove, {"id": gid, "tags": [MEMBER_TAG]}),
            "tagsRemove",
        )


def update_customers(
    customer_id: int | None = None,
    max_workers: int = 5,
    limit: int | None = None,
    only: str | None = None,
) -> dict[str, int]:
    """Create local-only customers in Shopify and push membership for dirty ones.

    New = no shopify_customer_id. Dirty = shopify_updated_at older than
    updated_at, which only membership columns can cause. Contact columns are
    sent on create only; after that Shopify owns them.
    """
    database = Database()
    shopify = Shopify()
    shopify.get_token()

    counts = {"created": 0, "updated": 0, "failed": 0}

    def _run(label: str, batch: list[dict], task) -> None:
        if not batch:
            return
        logger.info(f"{len(batch)} customer(s) to {label}")
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(task, row): row for row in batch}
            for fut in as_completed(futures):
                row = futures[fut]
                try:
                    fut.result()
                    counts["created" if label == "create" else "updated"] += 1
                except (CustomerPushError, ShopifyQueryError) as e:
                    counts["failed"] += 1
                    logger.error(
                        f"Failed to {label} customer {row['id']} ({row.get('email')}): {e}"
                    )

    def _create(row: dict) -> None:
        gid = create_customer(shopify, row)
        _mark_pushed(database, row["id"], gid)
        logger.debug(f"Created customer {row['id']} as {gid}")

    def _update(row: dict) -> None:
        push_membership(shopify, row)
        _mark_pushed(database, row["id"])
        logger.debug(f"Pushed membership for customer {row['id']}")

    if only in (None, "new"):
        _run("create", _fetch_new(database, customer_id, limit), _create)
    if only in (None, "dirty"):
        _run("update", _fetch_dirty(database, customer_id, limit), _update)

    logger.success(
        f"Customer push complete: {counts['created']} created, "
        f"{counts['updated']} updated, {counts['failed']} failed"
    )
    return counts
