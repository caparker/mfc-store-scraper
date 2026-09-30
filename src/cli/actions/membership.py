"""Edit membership info on azure.customers."""

from datetime import date

from psycopg import sql, rows

from src.db.postgres import Database

# Sentinel meaning "leave this column alone" (None means "clear it").
UNSET = object()


def set_membership(
    email: str,
    member_number=UNSET,
    membership_status=UNSET,
    member_since: date | None = UNSET,
    membership_expires: date | None = UNSET,
    notes=UNSET,
    first_name=UNSET,
    last_name=UNSET,
    phone=UNSET,
    create: bool = False,
) -> dict | None:
    """Update membership (and, for new local rows, contact) columns by email.

    Returns the row after the update, or None when no customer has that
    email and create is False. Contact columns are only written when the
    row has no Shopify id yet, since Shopify owns them after the create.
    """
    database = Database()

    changes = {
        "member_number": member_number,
        "membership_status": membership_status,
        "member_since": member_since,
        "membership_expires": membership_expires,
        "notes": notes,
    }
    contact = {"first_name": first_name, "last_name": last_name, "phone": phone}

    existing = database.fetchone(
        sql.SQL(
            """
            SELECT id, shopify_customer_id
            FROM azure.customers
            WHERE lower(email) = lower(%(email)s)
            """
        ),
        {"email": email},
        row_factory=rows.dict_row,
    )

    if existing is None:
        if not create:
            return None
        database.execute(
            sql.SQL("INSERT INTO azure.customers (email) VALUES (%(email)s)"),
            {"email": email},
        )
        existing = {"shopify_customer_id": None}

    if existing["shopify_customer_id"] is None:
        changes.update(contact)

    assignments = [
        sql.SQL("{} = {}").format(sql.Identifier(col), sql.Placeholder(col))
        for col, value in changes.items()
        if value is not UNSET
    ]
    if assignments:
        database.execute(
            sql.SQL(
                """
                UPDATE azure.customers
                SET {assignments}
                WHERE lower(email) = lower(%(email)s)
                """
            ).format(assignments=sql.SQL(", ").join(assignments)),
            {"email": email, **{c: v for c, v in changes.items() if v is not UNSET}},
        )

    return database.fetchone(
        sql.SQL(
            """
            SELECT
                id
                , shopify_customer_id
                , email
                , first_name
                , last_name
                , member_number
                , membership_status
                , member_since
                , membership_expires
                , notes
            FROM azure.customers
            WHERE lower(email) = lower(%(email)s)
            """
        ),
        {"email": email},
        row_factory=rows.dict_row,
    )
