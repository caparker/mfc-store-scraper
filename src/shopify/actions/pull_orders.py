"""Action for mirroring open Shopify orders into azure.orders / azure.order_items."""

from datetime import datetime, timedelta, timezone

from psycopg import sql

from src.db.postgres import Database
from src.lib.logger import logger
from src.shopify.queries import Queries
from src.shopify.shopify import Shopify, ShopifyQueryError

# Orders page x line items per order must stay under Shopify's 1000-point
# single-query cost limit; 10 x 40 (plus variant and customer objects) fits.
ORDER_PAGE_SIZE = 8
LINE_ITEM_PAGE_SIZE = 40
IDS_PAGE_SIZE = 8

# Unfulfilled in Shopify's search syntax means null or partially fulfilled.
OPEN_QUERY = "status:open AND fulfillment_status:unfulfilled"

# Overlap on the updated_at watermark so clock skew cannot skip an order.
WATERMARK_OVERLAP = timedelta(minutes=5)

REFUNDED_STATUSES = {"PARTIALLY_REFUNDED", "REFUNDED"}
# restockType values that mean "removed before fulfillment"; the rest are returns.
CANCEL_RESTOCK_TYPES = {"CANCEL"}

SKU_PREFIX = "AZ-"


def _check_errors(resp: dict) -> None:
    errors = resp.get("errors")
    if errors:
        raise ShopifyQueryError(f"{errors}")


def _packaging_code(sku: str | None) -> str | None:
    if sku and sku.startswith(SKU_PREFIX):
        return sku[len(SKU_PREFIX):]
    return None


def _remaining_line_items(shopify: Shopify, order_id: str, cursor: str) -> list[dict]:
    """Fetch line items past the first page for an order with many lines."""
    items: list[dict] = []
    while cursor:
        resp = shopify.query_file(
            Queries.order_line_items_page,
            {"id": order_id, "first": LINE_ITEM_PAGE_SIZE, "after": cursor},
        )
        _check_errors(resp)
        connection = resp["data"]["order"]["lineItems"]
        items.extend(connection["nodes"])
        page_info = connection["pageInfo"]
        cursor = page_info["endCursor"] if page_info["hasNextPage"] else None
    return items


def _complete_line_items(shopify: Shopify, node: dict) -> list[dict]:
    connection = node["lineItems"]
    items = list(connection["nodes"])
    if connection["pageInfo"]["hasNextPage"]:
        logger.debug(f"Order {node['name']} has more than {LINE_ITEM_PAGE_SIZE} line items")
        items.extend(
            _remaining_line_items(shopify, node["id"], connection["pageInfo"]["endCursor"])
        )
    return items


def _fetch_orders(shopify: Shopify, query: str) -> list[dict]:
    orders: list[dict] = []
    cursor = None
    while True:
        resp = shopify.query_file(
            Queries.orders_page,
            {"first": ORDER_PAGE_SIZE, "after": cursor, "query": query},
        )
        _check_errors(resp)
        connection = resp["data"]["orders"]
        orders.extend(connection["nodes"])
        page_info = connection["pageInfo"]
        if not page_info["hasNextPage"]:
            return orders
        cursor = page_info["endCursor"]


def _fetch_by_ids(shopify: Shopify, ids: list[str]) -> dict[str, dict | None]:
    """Return {shopify_order_id: node or None}; None means Shopify has no such order."""
    found: dict[str, dict | None] = {}
    for start in range(0, len(ids), IDS_PAGE_SIZE):
        chunk = ids[start:start + IDS_PAGE_SIZE]
        resp = shopify.query_file(Queries.orders_by_ids, {"ids": chunk})
        _check_errors(resp)
        for gid, node in zip(chunk, resp["data"]["nodes"]):
            found[gid] = node or None
    return found


def _fetch_refund_quantities(shopify: Shopify, order_ids: list[str]) -> dict[str, dict[str, int]]:
    """Return {line_item_id: {"cancelled": n, "returned": n}} for the given orders."""
    quantities: dict[str, dict[str, int]] = {}
    for start in range(0, len(order_ids), IDS_PAGE_SIZE):
        chunk = order_ids[start:start + IDS_PAGE_SIZE]
        resp = shopify.query_file(Queries.order_refunds_by_ids, {"ids": chunk})
        _check_errors(resp)
        for node in resp["data"]["nodes"]:
            for refund in (node or {}).get("refunds") or []:
                for rli in refund["refundLineItems"]["nodes"]:
                    line_id = (rli.get("lineItem") or {}).get("id")
                    if not line_id:
                        continue
                    bucket = quantities.setdefault(line_id, {"cancelled": 0, "returned": 0})
                    key = "cancelled" if rli.get("restockType") in CANCEL_RESTOCK_TYPES else "returned"
                    bucket[key] += rli.get("quantity") or 0
    return quantities


def _amount(money_set: dict | None) -> str | None:
    """Shop-currency amount from a MoneyBag, as the decimal string Shopify sends."""
    return ((money_set or {}).get("shopMoney") or {}).get("amount")


def _currency(money_set: dict | None) -> str | None:
    return ((money_set or {}).get("shopMoney") or {}).get("currencyCode")


def _customer_row(customer: dict | None) -> dict | None:
    if not customer:
        return None
    return {
        "gid": customer["id"],
        "email": (customer.get("defaultEmailAddress") or {}).get("emailAddress"),
        "first_name": customer.get("firstName"),
        "last_name": customer.get("lastName"),
        "phone": (customer.get("defaultPhoneNumber") or {}).get("phoneNumber"),
    }


def _order_row(node: dict, pulled_at: datetime) -> dict:
    customer = node.get("customer") or {}
    return {
        "gid": node["id"],
        "name": node["name"],
        "shopify_customer_id": customer.get("id"),
        "financial_status": node.get("displayFinancialStatus"),
        "fulfillment_status": node.get("displayFulfillmentStatus"),
        "ordered_at": node.get("createdAt"),
        "remote_updated_at": node.get("updatedAt"),
        "cancelled_at": node.get("cancelledAt"),
        "closed_at": node.get("closedAt"),
        "note": node.get("note"),
        "currency": _currency(node.get("currentTotalPriceSet")),
        "subtotal": _amount(node.get("currentSubtotalPriceSet")),
        "total_tax": _amount(node.get("currentTotalTaxSet")),
        "total_discounts": _amount(node.get("currentTotalDiscountsSet")),
        "total_shipping": _amount(node.get("totalShippingPriceSet")),
        "total": _amount(node.get("currentTotalPriceSet")),
        "net_payment": _amount(node.get("netPaymentSet")),
        "total_refunded": _amount(node.get("totalRefundedSet")),
        "pulled_at": pulled_at,
    }


def _item_row(order_gid: str, item: dict, refunded: dict[str, int] | None = None) -> dict:
    variant = item.get("variant") or {}
    refunded = refunded or {}
    return {
        "order_gid": order_gid,
        "gid": item["id"],
        "variant_gid": variant.get("id"),
        "sku": item.get("sku"),
        "packaging_code": _packaging_code(item.get("sku")),
        "title": item.get("title"),
        "variant_title": item.get("variantTitle"),
        "quantity": item.get("quantity") or 0,
        "current_quantity": item.get("currentQuantity"),
        "unfulfilled_quantity": item.get("unfulfilledQuantity") or 0,
        "cancelled_quantity": refunded.get("cancelled", 0),
        "returned_quantity": refunded.get("returned", 0),
        "original_unit_price": _amount(item.get("originalUnitPriceSet")),
        "discounted_unit_price": _amount(item.get("discountedUnitPriceSet")),
        "discounted_total": _amount(item.get("discountedTotalSet")),
    }


def _write(
    database: Database,
    customers: list[dict],
    orders: list[dict],
    items: list[dict],
) -> None:
    """Write everything in one transaction so a failed pull leaves the mirror intact."""
    with database.connection() as conn:
        with conn.cursor() as curs:
            if customers:
                # Stub rows for customers we have not pulled yet; contact info is
                # refreshed by pull-customers, so do nothing on conflict.
                curs.executemany(
                    sql.SQL(
                        """
                        INSERT INTO azure.customers (
                            shopify_customer_id, email, first_name, last_name, phone
                        )
                        VALUES (
                            %(gid)s, %(email)s, %(first_name)s, %(last_name)s, %(phone)s
                        )
                        ON CONFLICT (shopify_customer_id) DO NOTHING
                        """
                    ),
                    customers,
                )
            curs.executemany(
                sql.SQL(
                    """
                    INSERT INTO azure.orders (
                        shopify_order_id, name, customers_id, shopify_customer_id
                        , financial_status, fulfillment_status, ordered_at
                        , cancelled_at, closed_at, note
                        , currency, subtotal, total_tax, total_discounts
                        , total_shipping, total, net_payment, total_refunded
                        , remote_updated_at, last_pulled_at
                    )
                    VALUES (
                        %(gid)s, %(name)s
                        , (
                            SELECT id FROM azure.customers
                            WHERE shopify_customer_id = %(shopify_customer_id)s
                        )
                        , %(shopify_customer_id)s
                        , %(financial_status)s, %(fulfillment_status)s, %(ordered_at)s
                        , %(cancelled_at)s, %(closed_at)s, %(note)s
                        , %(currency)s, %(subtotal)s, %(total_tax)s, %(total_discounts)s
                        , %(total_shipping)s, %(total)s, %(net_payment)s, %(total_refunded)s
                        , %(remote_updated_at)s, %(pulled_at)s
                    )
                    ON CONFLICT (shopify_order_id) DO UPDATE SET
                        name = EXCLUDED.name
                        , customers_id = COALESCE(EXCLUDED.customers_id, azure.orders.customers_id)
                        , shopify_customer_id = EXCLUDED.shopify_customer_id
                        , financial_status = EXCLUDED.financial_status
                        , fulfillment_status = EXCLUDED.fulfillment_status
                        , ordered_at = EXCLUDED.ordered_at
                        , cancelled_at = EXCLUDED.cancelled_at
                        , closed_at = EXCLUDED.closed_at
                        , note = EXCLUDED.note
                        , currency = EXCLUDED.currency
                        , subtotal = EXCLUDED.subtotal
                        , total_tax = EXCLUDED.total_tax
                        , total_discounts = EXCLUDED.total_discounts
                        , total_shipping = EXCLUDED.total_shipping
                        , total = EXCLUDED.total
                        , net_payment = EXCLUDED.net_payment
                        , total_refunded = EXCLUDED.total_refunded
                        , remote_updated_at = EXCLUDED.remote_updated_at
                        , last_pulled_at = EXCLUDED.last_pulled_at
                    """
                ),
                orders,
            )
            if items:
                curs.executemany(
                    sql.SQL(
                        """
                        INSERT INTO azure.order_items (
                            orders_id, shopify_line_item_id, shopify_variant_id, sku
                            , packaging_code, title, variant_title
                            , quantity, current_quantity, unfulfilled_quantity
                            , cancelled_quantity, returned_quantity
                            , original_unit_price, discounted_unit_price, discounted_total
                        )
                        VALUES (
                            (SELECT id FROM azure.orders WHERE shopify_order_id = %(order_gid)s)
                            , %(gid)s, %(variant_gid)s, %(sku)s
                            , (SELECT code FROM azure.packaging WHERE code = %(packaging_code)s)
                            , %(title)s, %(variant_title)s
                            , %(quantity)s, %(current_quantity)s, %(unfulfilled_quantity)s
                            , %(cancelled_quantity)s, %(returned_quantity)s
                            , %(original_unit_price)s, %(discounted_unit_price)s
                            , %(discounted_total)s
                        )
                        ON CONFLICT (shopify_line_item_id) DO UPDATE SET
                            shopify_variant_id = EXCLUDED.shopify_variant_id
                            , sku = EXCLUDED.sku
                            , packaging_code = EXCLUDED.packaging_code
                            , title = EXCLUDED.title
                            , variant_title = EXCLUDED.variant_title
                            , quantity = EXCLUDED.quantity
                            , current_quantity = EXCLUDED.current_quantity
                            , unfulfilled_quantity = EXCLUDED.unfulfilled_quantity
                            , cancelled_quantity = EXCLUDED.cancelled_quantity
                            , returned_quantity = EXCLUDED.returned_quantity
                            , original_unit_price = EXCLUDED.original_unit_price
                            , discounted_unit_price = EXCLUDED.discounted_unit_price
                            , discounted_total = EXCLUDED.discounted_total
                        """
                    ),
                    items,
                )


def _watermark_query(database: Database) -> str | None:
    since = database.fetchone(
        sql.SQL("SELECT max(remote_updated_at) FROM azure.orders"), {}
    )[0]
    if since is None:
        return None
    since = since - WATERMARK_OVERLAP
    return f"updated_at:>='{since.isoformat()}'"


def pull_orders(full: bool = False) -> dict[str, int]:
    """Mirror Shopify orders into the DB.

    - Every order Shopify lists as open and unfulfilled is upserted with its
      line items. Line items whose SKU is `AZ-<code>` are linked to
      azure.packaging by code.
    - Every order updated in Shopify since the newest remote_updated_at we
      hold is also upserted, open or closed, so later refunds and edits are
      recorded. With full set (or on the first pull) every order in the
      read_orders window is fetched instead.
    - Locally open orders that Shopify no longer lists are re-fetched by id
      so their fulfillment, cancellation, or closure is recorded. Orders that
      no longer exist in Shopify are marked closed.
    - For refunded orders, refund line items are fetched so each line's
      cancelled (removed before fulfillment) and returned quantities are known.

    Orders are read-only mirrors: nothing here is ever pushed to Shopify.
    """
    logger.info("Pulling orders from Shopify")

    database = Database()
    shopify = Shopify()
    pulled_at = datetime.now(timezone.utc)

    remote: dict[str, dict] = {node["id"]: node for node in _fetch_orders(shopify, OPEN_QUERY)}
    open_ids = set(remote)
    logger.info(f"{len(remote)} open order(s) in Shopify")

    changed_query = None if full else _watermark_query(database)
    if changed_query is None:
        logger.info("No watermark; fetching every order Shopify will return")
    else:
        logger.info(f"Fetching orders with {changed_query}")
    changed = _fetch_orders(shopify, changed_query)
    new_changed = [n for n in changed if n["id"] not in remote]
    remote.update({n["id"]: n for n in changed})
    logger.info(f"{len(changed)} updated order(s), {len(new_changed)} not already in the open set")

    local_open = [
        gid for (gid,) in database.fetchall(
            sql.SQL("SELECT shopify_order_id FROM azure.open_orders"), {}
        )
    ]
    missing = [gid for gid in local_open if gid not in remote]
    vanished: list[str] = []
    if missing:
        logger.info(f"{len(missing)} locally open order(s) not returned by Shopify; refreshing")
        for gid, node in _fetch_by_ids(shopify, missing).items():
            if node is None:
                vanished.append(gid)
                logger.warning(f"Order {gid} no longer exists in Shopify; marking closed")
                continue
            remote[gid] = node

    refunded_ids = [
        gid for gid, node in remote.items()
        if node.get("displayFinancialStatus") in REFUNDED_STATUSES
    ]
    refunds = _fetch_refund_quantities(shopify, refunded_ids) if refunded_ids else {}
    if refunded_ids:
        logger.info(f"{len(refunded_ids)} refunded order(s); {len(refunds)} refunded line item(s)")

    customers: dict[str, dict] = {}
    orders: list[dict] = []
    items: list[dict] = []
    unlinked = 0
    for node in remote.values():
        customer = _customer_row(node.get("customer"))
        if customer:
            customers[customer["gid"]] = customer
        orders.append(_order_row(node, pulled_at))
        for item in _complete_line_items(shopify, node):
            row = _item_row(node["id"], item, refunds.get(item["id"]))
            if row["packaging_code"] is None:
                unlinked += 1
            items.append(row)

    _write(database, list(customers.values()), orders, items)

    if vanished:
        database.execute(
            sql.SQL(
                """
                UPDATE azure.orders
                SET closed_at = now()
                    , last_pulled_at = %(pulled_at)s
                WHERE shopify_order_id = ANY(%(ids)s)
                """
            ),
            {"pulled_at": pulled_at, "ids": vanished},
        )

    still_open = database.fetchone(
        sql.SQL("SELECT count(*) FROM azure.open_orders"), {}
    )[0]

    counts = {
        "pulled": len(orders),
        "open_remote": len(open_ids),
        "changed": len(new_changed),
        "line_items": len(items),
        "unlinked_items": unlinked,
        "refunded": len(refunded_ids),
        "closed": len(missing),
        "open": still_open,
    }
    logger.success(
        f"Order pull complete: {counts['pulled']} order(s) written "
        f"({counts['open_remote']} open, {counts['changed']} changed closed), "
        f"{counts['line_items']} line item(s) ({counts['unlinked_items']} not Azure), "
        f"{counts['refunded']} refunded, {counts['closed']} left the open set, "
        f"{counts['open']} open locally"
    )
    return counts
