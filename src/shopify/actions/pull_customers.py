"""Action for pulling customer contact info from Shopify into azure.customers."""

from psycopg import sql
from psycopg.types.json import Jsonb

from src.db.postgres import Database
from src.lib.logger import logger
from src.shopify.queries import Queries
from src.shopify.shopify import Shopify, ShopifyQueryError

PAGE_SIZE = 250


def _check_errors(resp: dict) -> None:
    errors = resp.get("errors")
    if errors:
        raise ShopifyQueryError(f"{errors}")


def _fetch_customers(shopify: Shopify, query: str | None) -> list[dict]:
    customers: list[dict] = []
    cursor = None
    while True:
        resp = shopify.query_file(
            Queries.customers_page,
            {"first": PAGE_SIZE, "after": cursor, "query": query},
        )
        _check_errors(resp)
        connection = resp["data"]["customers"]
        customers.extend(connection["nodes"])
        page_info = connection["pageInfo"]
        if not page_info["hasNextPage"]:
            return customers
        cursor = page_info["endCursor"]


def _row(node: dict) -> dict:
    address = node.get("defaultAddress")
    return {
        "gid": node["id"],
        "email": (node.get("defaultEmailAddress") or {}).get("emailAddress"),
        "first_name": node.get("firstName"),
        "last_name": node.get("lastName"),
        "phone": (node.get("defaultPhoneNumber") or {}).get("phoneNumber"),
        "default_address": Jsonb(address) if address else None,
        "remote_updated_at": node.get("updatedAt"),
    }


def pull_customers(full: bool = False) -> dict[str, int]:
    """Pull customer contact info from Shopify.

    Only customers Shopify reports as updated since the newest
    remote_updated_at we hold are fetched, unless full is set. Rows are
    matched on shopify_customer_id; a row created locally that has no
    Shopify id yet is linked by email instead of duplicated.

    Contact columns are excluded from the dirty trigger, so a pull never
    marks a customer dirty. Membership columns are never read from Shopify.
    """
    logger.info("Pulling customers from Shopify")

    database = Database()
    shopify = Shopify()

    query = None
    if not full:
        since = database.fetchone(
            sql.SQL("SELECT max(remote_updated_at) FROM azure.customers"), {}
        )[0]
        if since is not None:
            query = f"updated_at:>='{since.isoformat()}'"
            logger.info(f"Fetching customers updated since {since.isoformat()}")

    nodes = _fetch_customers(shopify, query)
    logger.info(f"{len(nodes)} customer(s) returned by Shopify")
    if not nodes:
        return {"pulled": 0, "linked": 0}

    rows = [_row(n) for n in nodes]
    with_email = [r for r in rows if r["email"]]

    with database.connection() as conn:
        with conn.cursor() as curs:
            linked = 0
            if with_email:
                # Attach Shopify ids to rows that were created locally and
                # also exist in Shopify under the same email.
                curs.executemany(
                    sql.SQL(
                        """
                        UPDATE azure.customers
                        SET shopify_customer_id = %(gid)s
                        WHERE shopify_customer_id IS NULL
                          AND lower(email) = lower(%(email)s)
                        """
                    ),
                    with_email,
                    returning=False,
                )
                linked = curs.rowcount if curs.rowcount > 0 else 0

            curs.executemany(
                sql.SQL(
                    """
                    INSERT INTO azure.customers (
                        shopify_customer_id, email, first_name, last_name, phone
                        , default_address, remote_updated_at
                    )
                    VALUES (
                        %(gid)s, %(email)s, %(first_name)s, %(last_name)s, %(phone)s
                        , %(default_address)s, %(remote_updated_at)s
                    )
                    ON CONFLICT (shopify_customer_id) DO UPDATE SET
                        email = EXCLUDED.email
                        , first_name = EXCLUDED.first_name
                        , last_name = EXCLUDED.last_name
                        , phone = EXCLUDED.phone
                        , default_address = EXCLUDED.default_address
                        , remote_updated_at = EXCLUDED.remote_updated_at
                    """
                ),
                rows,
            )

    counts = {"pulled": len(rows), "linked": linked}
    logger.success(
        f"Customer pull complete: {counts['pulled']} customer(s) written, "
        f"{counts['linked']} local row(s) linked by email"
    )
    return counts
