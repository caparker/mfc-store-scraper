"""Build the supplier purchase list from open orders, and record supplier orders."""

from psycopg import sql, rows

from src.db.postgres import Database
from src.lib.logger import logger

AZURE = "azure"


def get_purchase_list(database: Database | None = None) -> list[dict]:
    """Outstanding demand per variant (Azure items first, then unlinked)."""
    database = database or Database()
    return database.fetchall(
        sql.SQL("SELECT * FROM azure.purchase_list"), {}, row_factory=rows.dict_row
    )


def get_purchase_demand(database: Database | None = None) -> list[dict]:
    """Outstanding demand per order line item."""
    database = database or Database()
    return database.fetchall(
        sql.SQL(
            """
            SELECT *
            FROM azure.purchase_demand
            WHERE outstanding > 0
            ORDER BY variants_id IS NULL, variant_code, ordered_at
            """
        ),
        {},
        row_factory=rows.dict_row,
    )


def commit_supplier_order(
    supplier: str = AZURE,
    notes: str | None = None,
    database: Database | None = None,
    variant_codes: list[str] | None = None,
) -> dict[str, int]:
    """Record the current outstanding demand as a placed supplier order.

    One supplier_order_items row is written per variant (or per SKU for
    unlinked items) with the summed quantity and the Azure retail price
    current at commit time as unit_price. supplier_order_allocations rows tie
    each variant row back to the customer line items it covers.

    For the azure supplier only line items linked to azure.variants are
    recorded; unlinked items are not something Azure can supply. For any
    other supplier name, only the unlinked items are recorded. With
    variant_codes, only those codes are recorded (used by quick-order to
    commit exactly what made it into the cart). Afterwards the purchase list
    shows only demand that arrived since.
    """
    database = database or Database()
    nothing = {"supplier_order_id": 0, "line_items": 0, "units": 0, "total_cost": 0}

    if variant_codes is not None:
        if not variant_codes:
            logger.info("No variant codes to commit")
            return nothing
        item_filter = sql.SQL("pd.variant_code = ANY(%(codes)s)")
    elif supplier == AZURE:
        item_filter = sql.SQL("pd.variants_id IS NOT NULL")
    else:
        item_filter = sql.SQL("pd.variants_id IS NULL")

    with database.connection() as conn:
        with conn.cursor() as curs:
            curs.execute(
                sql.SQL(
                    """
                    INSERT INTO azure.supplier_orders (supplier, notes)
                    VALUES (%(supplier)s, %(notes)s)
                    RETURNING id
                    """
                ),
                {"supplier": supplier, "notes": notes},
            )
            supplier_order_id = curs.fetchone()[0]
            params = {"supplier_order_id": supplier_order_id, "codes": variant_codes}

            curs.execute(
                sql.SQL(
                    """
                    INSERT INTO azure.supplier_order_items (
                        supplier_orders_id, variants_id, sku, quantity, unit_price
                    )
                    SELECT %(supplier_order_id)s
                      , pd.variants_id
                      , pd.sku
                      , sum(pd.outstanding)
                      , cp.retail_dollars::numeric(12, 2)
                    FROM azure.purchase_demand pd
                    LEFT JOIN azure.current_prices cp ON cp.variants_id = pd.variants_id
                    WHERE pd.outstanding > 0
                      AND {item_filter}
                    GROUP BY pd.variants_id, pd.sku, cp.retail_dollars
                    RETURNING quantity, unit_price
                    """
                ).format(item_filter=item_filter),
                params,
            )
            items = curs.fetchall()

            if not items:
                # Nothing to record; do not leave an empty supplier order behind.
                conn.rollback()
                logger.info(f"No outstanding {supplier} demand to commit")
                return nothing

            # purchase_demand still reflects pre-commit demand here because the
            # allocations that would reduce it are written by this statement.
            curs.execute(
                sql.SQL(
                    """
                    INSERT INTO azure.supplier_order_allocations (
                        supplier_order_items_id, order_items_id, quantity
                    )
                    SELECT soi.id, pd.order_items_id, pd.outstanding
                    FROM azure.purchase_demand pd
                    JOIN azure.supplier_order_items soi
                      ON soi.supplier_orders_id = %(supplier_order_id)s
                     AND soi.variants_id IS NOT DISTINCT FROM pd.variants_id
                     AND soi.sku IS NOT DISTINCT FROM pd.sku
                    WHERE pd.outstanding > 0
                      AND {item_filter}
                    """
                ).format(item_filter=item_filter),
                params,
            )

    counts = {
        "supplier_order_id": supplier_order_id,
        "line_items": len(items),
        "units": sum(q for q, _ in items),
        "total_cost": sum(q * price for q, price in items if price is not None),
    }
    logger.success(
        f"Recorded supplier order {supplier_order_id} ({supplier}): "
        f"{counts['line_items']} variant(s), {counts['units']} unit(s), "
        f"${counts['total_cost']:.2f}"
    )
    return counts
