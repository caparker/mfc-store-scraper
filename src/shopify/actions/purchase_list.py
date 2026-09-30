"""Build the supplier purchase list from open orders, and record supplier orders."""

from psycopg import sql, rows

from src.db.postgres import Database
from src.lib.logger import logger

AZURE = "azure"


def get_purchase_list(database: Database | None = None) -> list[dict]:
    """Outstanding demand per packaging code (Azure items first, then unlinked)."""
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
            ORDER BY packaging_code IS NULL, packaging_code, ordered_at
            """
        ),
        {},
        row_factory=rows.dict_row,
    )


def commit_supplier_order(
    supplier: str = AZURE,
    notes: str | None = None,
    database: Database | None = None,
) -> dict[str, int]:
    """Record the current outstanding demand as a placed supplier order.

    For the azure supplier only line items linked to azure.packaging are
    recorded; unlinked items are not something Azure can supply. For any
    other supplier name, only the unlinked items are recorded. Afterwards the
    purchase list shows only demand that arrived since.
    """
    database = database or Database()

    if supplier == AZURE:
        item_filter = sql.SQL("packaging_code IS NOT NULL")
    else:
        item_filter = sql.SQL("packaging_code IS NULL")

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

            curs.execute(
                sql.SQL(
                    """
                    INSERT INTO azure.supplier_order_items (
                        supplier_orders_id, order_items_id, packaging_code, quantity
                    )
                    SELECT %(supplier_order_id)s, order_items_id, packaging_code, outstanding
                    FROM azure.purchase_demand
                    WHERE outstanding > 0
                      AND {item_filter}
                    RETURNING quantity
                    """
                ).format(item_filter=item_filter),
                {"supplier_order_id": supplier_order_id},
            )
            quantities = [q for (q,) in curs.fetchall()]

            if not quantities:
                # Nothing to record; do not leave an empty supplier order behind.
                conn.rollback()
                logger.info(f"No outstanding {supplier} demand to commit")
                return {"supplier_order_id": 0, "line_items": 0, "units": 0}

    counts = {
        "supplier_order_id": supplier_order_id,
        "line_items": len(quantities),
        "units": sum(quantities),
    }
    logger.success(
        f"Recorded supplier order {supplier_order_id} ({supplier}): "
        f"{counts['line_items']} line item(s), {counts['units']} unit(s)"
    )
    return counts
