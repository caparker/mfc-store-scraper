from datetime import datetime

import typer

from src.shopify.actions import (
    add_products,
    update_products,
    update_variants,
    update_stock,
    sync_media,
    pull_media,
    dump_database,
    run_pipeline,
    set_product_status,
    pull_product_status,
    pull_orders,
    pull_customers,
    update_customers,
    get_purchase_list,
    get_purchase_demand,
    commit_supplier_order,
)
from src.cli.actions.status import sync_status, sync_samples
from src.cli.actions.membership import set_membership, UNSET
from src.db import queries as db_queries
from src.shopify.shopify import Shopify, ShopifyConnectionError
from src.shopify.types.models.product import ProductStatus


app = typer.Typer()

# Scopes every command in this CLI relies on.
REQUIRED_SCOPES = {
    "read_products",
    "write_products",
    "read_inventory",
    "write_inventory",
    "read_orders",
    "read_customers",
    "write_customers",
}


@app.command()
def check_connection():
    """Verify Shopify credentials and reachability."""
    shop = Shopify()
    try:
        info = shop.check_connection()
        installation = info.get("currentAppInstallation", {})
        app_name = installation.get("app", {}).get("title", "?")
        typer.echo(f"✓ Connected to {shop.shop_domain} as {app_name}")
    except ShopifyConnectionError as e:
        typer.echo(f"✗ Connection failed: {e}", err=True)
        raise typer.Exit(code=1)

    granted = {s["handle"] for s in installation.get("accessScopes", [])}
    missing = sorted(REQUIRED_SCOPES - granted)
    if missing:
        typer.echo(
            f"✗ Missing access scopes: {', '.join(missing)} "
            "(add them to the app and regenerate the token)",
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(f"✓ Access scopes: {', '.join(sorted(granted))}")


@app.command()
def run():
    """Run the full pipeline: scrape → sync products/variants/images/stock → pull customers
    → sync customers → pull orders → dump."""
    run_pipeline()


@app.command()
def dump_db(
    output_dir: str = typer.Option(
        "./dumps", "--output-dir", help="Directory to write the dump to"
    ),
    include_customers: bool = typer.Option(
        False, "--include-customers",
        help="Also dump customer and order rows (personal data)",
    ),
):
    """Dump the Postgres database to a timestamped SQL file. Customer and order rows
    are excluded by default."""
    dump_database(output_dir=output_dir, include_customers=include_customers)


@app.command()
def sync_variants(
    packaging_code: str = typer.Option(
        None, "--packaging-code", help="Only update this azure.packaging.code"
    ),
    product_id: int = typer.Option(
        None, "--product-id", help="Update all variants for this azure.products.id"
    ),
    max_workers: int = typer.Option(
        5, "--max-workers", help="Number of parallel Shopify requests"
    ),
    limit: int = typer.Option(
        None, "--limit", help="Only process the first N rows"
    ),
):
    """Push dirty azure.packaging rows to Shopify (price, cost, inventory policy). Stock is sync-stock."""
    update_variants(
        packaging_code=packaging_code,
        product_id=product_id,
        max_workers=max_workers,
        limit=limit,
    )


@app.command()
def sync_stock(
    packaging_code: str = typer.Option(
        None, "--packaging-code", help="Only push this azure.packaging.code"
    ),
    product_id: int = typer.Option(
        None, "--product-id", help="Push all variants for this azure.products.id"
    ),
    max_workers: int = typer.Option(
        3, "--max-workers", help="Number of parallel Shopify requests"
    ),
    limit: int = typer.Option(
        None, "--limit", help="Only process the first N rows"
    ),
):
    """Push changed stock to Shopify in batches of 250 variants."""
    update_stock(
        packaging_code=packaging_code,
        product_id=product_id,
        max_workers=max_workers,
        limit=limit,
    )

@app.command("sync-media")
def sync_media_cmd(
    packaging_code: str = typer.Option(
        None, "--packaging-code", help="Only sync the image for this azure.packaging.code"
    ),
    product_id: int = typer.Option(
        None, "--product-id",
        help="Sync every variant image for this azure.products.id (retries failed ones)",
    ),
    max_workers: int = typer.Option(
        3, "--max-workers", help="Number of products handled in parallel"
    ),
    limit: int = typer.Option(
        None, "--limit", help="Only process the first N images"
    ),
):
    """Create each variant's first Azure image on its Shopify product and set it as the
    variant image."""
    counts = sync_media(
        product_id=product_id,
        packaging_code=packaging_code,
        max_workers=max_workers,
        limit=limit,
    )
    typer.echo(f"  uploaded:            {counts.get('uploaded', 0):>6}")
    typer.echo(f"  reused on product:   {counts.get('reused', 0):>6}")
    typer.echo(f"  ready:               {counts.get('ready', 0):>6}")
    typer.echo(f"  variant images set:  {counts.get('variants', 0):>6}")
    typer.echo(f"  detached:            {counts.get('detached', 0):>6}")
    typer.echo(f"  failed:              {counts.get('failed', 0):>6}")


@app.command("pull-media")
def pull_media_cmd(
    product_id: int = typer.Option(
        None, "--product-id", help="Only reconcile this azure.products.id"
    ),
    limit: int = typer.Option(
        None, "--limit", help="Only reconcile the first N products"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report differences without writing anywhere"
    ),
):
    """Match azure.media to the media actually on each Shopify product, by file name."""
    counts = pull_media(product_id=product_id, limit=limit, dry_run=dry_run)
    typer.echo(f"  products checked:        {counts['products']:>6}")
    typer.echo(f"  rows matched:            {counts['matched']:>6}")
    typer.echo(f"  rows detached:           {counts['detached']:>6}")
    typer.echo(f"  variant images set:      {counts['variant_set']:>6}")
    typer.echo(f"  variant images cleared:  {counts['variant_cleared']:>6}")
    typer.echo(f"  Shopify media unmatched: {counts['unmatched_in_shopify']:>6}")
    if counts["incomplete_products"]:
        typer.echo(
            f"  products with more media than checked: {counts['incomplete_products']}"
        )
    if counts["missing_products"]:
        typer.echo(f"  products missing in Shopify: {counts['missing_products']}")


@app.command()
def sync_products(
    product_id: int = typer.Option(
        None, "--product-id", help="Only sync this azure.products.id"
    ),
    max_workers: int = typer.Option(
        5, "--max-workers", help="Number of parallel Shopify requests"
    ),
    only: str = typer.Option(
        None, "--only",
        help="Restrict to 'new' (create only) or 'dirty' (update only)",
    ),
    limit: int = typer.Option(
        None, "--limit", help="Only process the first N rows"
    ),
    draft: bool = typer.Option(
        True, "--draft/--no-draft",
        help="Create new products as DRAFT (default) or ACTIVE",
    ),
):
    """Create new products and push dirty ones to Shopify."""
    if only not in (None, "new", "dirty"):
        typer.echo("--only must be 'new' or 'dirty'")
        raise typer.Exit(code=1)

    if only in (None, "new"):
        add_products(
            product_id=product_id,
            max_workers=max_workers,
            limit=limit,
            status=ProductStatus.DRAFT if draft else ProductStatus.ACTIVE,
        )

    if only in (None, "dirty"):
        update_products(product_id=product_id, max_workers=max_workers, limit=limit)


@app.command()
def set_status(
    status: ProductStatus = typer.Argument(
        ..., case_sensitive=False, help="ACTIVE, DRAFT, or ARCHIVED"
    ),
    product_id: int = typer.Option(
        None, "--product-id", help="Only change this azure.products.id"
    ),
    push: bool = typer.Option(
        True, "--push/--no-push",
        help="Push the change to Shopify now (default) or just mark rows dirty",
    ),
    max_workers: int = typer.Option(
        5, "--max-workers", help="Number of parallel Shopify requests"
    ),
):
    """Set the Shopify status of one product, or of every product not already in that status."""
    if status is ProductStatus.DELETED:
        typer.echo("DELETED is set by pull-status, not by hand", err=True)
        raise typer.Exit(code=1)

    changed = set_product_status(
        status=status,
        product_id=product_id,
        push=push,
        max_workers=max_workers,
    )
    if changed and not push:
        typer.echo(
            f"{changed} product(s) marked dirty; run `sync-products --only dirty` to push"
        )


@app.command()
def pull_status(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report differences without writing anywhere"
    ),
    delete_duplicates: bool = typer.Option(
        False, "--delete-duplicates",
        help="Delete non-ACTIVE Shopify duplicates of products the DB links elsewhere",
    ),
    include_active: bool = typer.Option(
        False, "--include-active",
        help="With --delete-duplicates, also delete ACTIVE duplicates",
    ),
):
    """Reconcile local product status with Shopify: pull statuses, mark deleted, report orphans and duplicates."""
    if include_active and not delete_duplicates:
        typer.echo("--include-active requires --delete-duplicates", err=True)
        raise typer.Exit(code=1)

    counts = pull_product_status(
        dry_run=dry_run,
        delete_duplicates=delete_duplicates,
        include_active=include_active,
    )
    typer.echo(f"  status changes:      {counts['changed']:>6}")
    typer.echo(f"  deleted in Shopify:  {counts['deleted']:>6}")
    typer.echo(f"  orphans:             {counts['orphans']:>6}")
    typer.echo(f"  duplicates:          {counts['duplicates']:>6}")
    typer.echo(f"  duplicates deleted:  {counts['duplicates_deleted']:>6}")


@app.command()
def status(
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Also show a sample of dirty rows"
    ),
    sample_size: int = typer.Option(
        10, "--n", help="How many rows to show per category when --verbose"
    ),
):
    """Show a summary of items pending sync to Shopify."""
    counts = sync_status()
    pending_keys = (
        "products_new", "products_dirty", "variants_new", "variants_dirty",
        "stock_dirty", "media_pending", "customers_new", "customers_dirty",
    )
    total = sum(counts[k] for k in pending_keys)

    typer.echo("Sync status:")
    typer.echo(f"  products new:    {counts['products_new']:>6}")
    typer.echo(f"  products dirty:  {counts['products_dirty']:>6}")
    typer.echo(f"  variants new:    {counts['variants_new']:>6}")
    typer.echo(f"  variants dirty:  {counts['variants_dirty']:>6}")
    typer.echo(f"  stock dirty:     {counts['stock_dirty']:>6}")
    typer.echo(f"  images pending:  {counts['media_pending']:>6}")
    typer.echo(f"  customers new:   {counts['customers_new']:>6}")
    typer.echo(f"  customers dirty: {counts['customers_dirty']:>6}")
    typer.echo(f"  total pending:   {total:>6}")
    typer.echo(f"  products deleted in Shopify (not synced): {counts['products_deleted']}")
    typer.echo(f"  images failed (not retried): {counts['media_failed']}")
    typer.echo("Orders:")
    typer.echo(f"  open orders:     {counts['orders_open']:>6}")
    typer.echo(
        f"  to purchase:     {counts['purchase_items']:>6} line item(s), "
        f"{counts['purchase_units']} unit(s)"
    )

    if not verbose:
        return

    samples = sync_samples(limit=sample_size)

    if samples["products_new"]:
        typer.echo("\nProducts to create:")
        for pid, name in samples["products_new"]:
            typer.echo(f"  [{pid}] {name}")

    if samples["products_dirty"]:
        typer.echo("\nProducts to update:")
        for pid, name, changed in samples["products_dirty"]:
            fields = ", ".join(changed) if changed else "?"
            typer.echo(f"  [{pid}] {name}  ({fields})")

    if samples["variants_new"]:
        typer.echo("\nVariants to create:")
        for pack_id, code, name in samples["variants_new"]:
            typer.echo(f"  [{pack_id}] {code}  {name}")

    if samples["variants_dirty"]:
        typer.echo("\nVariants to update:")
        for pack_id, code, name, changed in samples["variants_dirty"]:
            fields = ", ".join(changed) if changed else "?"
            typer.echo(f"  [{pack_id}] {code}  {name}  ({fields})")

    if samples["stock_dirty"]:
        typer.echo("\nStock to push:")
        for pack_id, code, name, stock, shopify_stock in samples["stock_dirty"]:
            typer.echo(f"  [{pack_id}] {code}  {name}  ({shopify_stock} -> {stock})")

    if samples["media_failed"]:
        typer.echo("\nImages failed:")
        for media_id, code, name, attempts, error in samples["media_failed"]:
            typer.echo(f"  [{media_id}] {code}  {name}  ({attempts} attempt(s): {error})")

    if samples["customers_new"]:
        typer.echo("\nCustomers to create:")
        for cid, email, member_number in samples["customers_new"]:
            typer.echo(f"  [{cid}] {email}  member {member_number or '-'}")

    if samples["customers_dirty"]:
        typer.echo("\nCustomers to update:")
        for cid, email, member_number, changed in samples["customers_dirty"]:
            fields = ", ".join(changed) if changed else "?"
            typer.echo(f"  [{cid}] {email}  member {member_number or '-'}  ({fields})")


@app.command("pull-orders")
def pull_orders_cmd(
    full: bool = typer.Option(
        False, "--full",
        help="Fetch every order Shopify will return, not just open and recently updated ones",
    ),
):
    """Mirror open and recently updated Shopify orders into azure.orders, with line item status."""
    counts = pull_orders(full=full)
    typer.echo(f"  orders written:        {counts['pulled']:>6}")
    typer.echo(f"    open in Shopify:     {counts['open_remote']:>6}")
    typer.echo(f"    changed, not open:   {counts['changed']:>6}")
    typer.echo(f"  line items written:    {counts['line_items']:>6}")
    typer.echo(f"  non-Azure line items:  {counts['unlinked_items']:>6}")
    typer.echo(f"  refunded orders:       {counts['refunded']:>6}")
    typer.echo(f"  left the open set:     {counts['closed']:>6}")
    typer.echo(f"  open locally:          {counts['open']:>6}")


@app.command("pull-customers")
def pull_customers_cmd(
    full: bool = typer.Option(
        False, "--full",
        help="Fetch every customer instead of only those updated since the last pull",
    ),
):
    """Pull customer contact info from Shopify. Membership columns are never overwritten."""
    counts = pull_customers(full=full)
    typer.echo(f"  customers written:     {counts['pulled']:>6}")
    typer.echo(f"  linked by email:       {counts['linked']:>6}")


@app.command()
def sync_customers(
    customer_id: int = typer.Option(
        None, "--customer-id", help="Only push this azure.customers.id (forces a push)"
    ),
    max_workers: int = typer.Option(
        5, "--max-workers", help="Number of parallel Shopify requests"
    ),
    only: str = typer.Option(
        None, "--only", help="Restrict to 'new' (create only) or 'dirty' (update only)"
    ),
    limit: int = typer.Option(
        None, "--limit", help="Only process the first N rows"
    ),
):
    """Create local-only customers in Shopify and push membership metafields for dirty
    ones."""
    if only not in (None, "new", "dirty"):
        typer.echo("--only must be 'new' or 'dirty'", err=True)
        raise typer.Exit(code=1)
    update_customers(
        customer_id=customer_id, max_workers=max_workers, limit=limit, only=only
    )


CONTACT_HELP = "Only used when creating a new local customer"


def _parse_date(value: str | None):
    if value is None:
        return UNSET
    if value == "":
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def _clearable(value: str | None):
    """Option not given -> leave alone; given as '' -> clear; otherwise set."""
    if value is None:
        return UNSET
    return value or None


@app.command("set-membership")
def set_membership_cmd(
    email: str = typer.Argument(..., help="Customer email (case-insensitive)"),
    member_number: str = typer.Option(None, "--member-number", help="Pass '' to clear"),
    membership_status: str = typer.Option(
        None, "--status",
        help="e.g. active, lapsed; 'active' adds the member tag in Shopify. Pass '' to clear",
    ),
    member_since: str = typer.Option(None, "--since", help="YYYY-MM-DD, or '' to clear"),
    membership_expires: str = typer.Option(None, "--expires", help="YYYY-MM-DD, or '' to clear"),
    notes: str = typer.Option(None, "--notes", help="Local-only notes. Pass '' to clear"),
    first_name: str = typer.Option(None, "--first-name", help=CONTACT_HELP),
    last_name: str = typer.Option(None, "--last-name", help=CONTACT_HELP),
    phone: str = typer.Option(None, "--phone", help=CONTACT_HELP),
    create: bool = typer.Option(
        False, "--create", help="Create the customer locally if no row has this email"
    ),
    push: bool = typer.Option(
        True, "--push/--no-push",
        help="Push to Shopify now (default) or just mark the row dirty",
    ),
):
    """Set membership info for a customer by email, creating them locally with --create."""
    try:
        row = set_membership(
            email=email,
            member_number=_clearable(member_number),
            membership_status=_clearable(membership_status),
            member_since=_parse_date(member_since),
            membership_expires=_parse_date(membership_expires),
            notes=_clearable(notes),
            first_name=_clearable(first_name),
            last_name=_clearable(last_name),
            phone=_clearable(phone),
            create=create,
        )
    except ValueError as e:
        typer.echo(f"Bad date: {e}", err=True)
        raise typer.Exit(code=1)

    if row is None:
        typer.echo(f"No customer with email {email}; pass --create to add one", err=True)
        raise typer.Exit(code=1)

    typer.echo(
        f"[{row['id']}] {row['email']}  {row['first_name'] or ''} {row['last_name'] or ''}".rstrip()
    )
    typer.echo(f"  member number: {row['member_number'] or '-'}")
    typer.echo(f"  status:        {row['membership_status'] or '-'}")
    typer.echo(f"  since:         {row['member_since'] or '-'}")
    typer.echo(f"  expires:       {row['membership_expires'] or '-'}")
    typer.echo(f"  notes:         {row['notes'] or '-'}")

    if push:
        update_customers(customer_id=row["id"])
    else:
        typer.echo("Row updated locally; run `sync-customers` to push")


@app.command()
def purchase_list(
    commit: bool = typer.Option(
        False, "--commit",
        help="Record the listed demand as a placed supplier order so it drops off the list",
    ),
    supplier: str = typer.Option(
        "azure", "--supplier",
        help="Supplier name for --commit. 'azure' commits linked items; "
             "anything else commits unlinked items",
    ),
    notes: str = typer.Option(None, "--notes", help="Notes to store on the supplier order"),
    by_order: bool = typer.Option(
        False, "--by-order", help="List per customer order line instead of per product"
    ),
):
    """Show what to buy from suppliers to fill open orders, based on the last pull-orders."""
    if by_order:
        demand = get_purchase_demand()
        if not demand:
            typer.echo("Nothing outstanding")
        for d in demand:
            code = d["packaging_code"] or f"(not Azure: {d['sku'] or '-'})"
            typer.echo(
                f"  {d['order_name']:<8} {code:<24} x{d['outstanding']:<4} "
                f"{d['title']} {d['variant_title'] or ''}  [{d['financial_status']}]"
            )
    else:
        items = get_purchase_list()
        if not items:
            typer.echo("Nothing outstanding")
        azure_items = [i for i in items if i["packaging_code"]]
        other_items = [i for i in items if not i["packaging_code"]]

        if azure_items:
            typer.echo("Azure Standard:")
            total = 0.0
            for i in azure_items:
                price = i["wholesale_dollars"]
                line_total = (price or 0) * int(i["outstanding"])
                total += line_total
                price_text = f"${price:>8.2f}" if price is not None else "        -"
                typer.echo(
                    f"  {i['packaging_code']:<12} x{int(i['outstanding']):<4} "
                    f"{price_text} {i['product_name']} — {i['size']}  "
                    f"(stock {i['azure_stock']}, {i['order_count']} order(s))"
                )
            typer.echo(f"  estimated wholesale total: ${total:,.2f}")

        if other_items:
            typer.echo("Not from Azure:")
            for i in other_items:
                typer.echo(
                    f"  {i['sku'] or '-':<12} x{int(i['outstanding']):<4} "
                    f"{i['product_name']} — {i['size'] or ''}  ({i['order_count']} order(s))"
                )

    if commit:
        counts = commit_supplier_order(supplier=supplier, notes=notes)
        if counts["supplier_order_id"]:
            typer.echo(
                f"Recorded supplier order {counts['supplier_order_id']} for {supplier}: "
                f"{counts['line_items']} variant(s), {counts['units']} unit(s), "
                f"${counts['total_cost']:.2f}"
            )



@app.command()
def query(
    name: str = typer.Argument(
        None, help="Named query to run (see --list)"
    ),
    raw_sql: str = typer.Option(
        None, "--sql", help="Run this SELECT instead of a named query"
    ),
    where: str = typer.Option(
        None, "--where", help="WHERE clause applied to the query's output columns"
    ),
    order_by: str = typer.Option(
        None, "--order-by", help="ORDER BY clause applied to the query's output columns"
    ),
    limit: int = typer.Option(
        db_queries.DEFAULT_LIMIT, "--limit", help="Max rows; 0 for no limit"
    ),
    output_format: str = typer.Option(
        "table", "--format", "-f", help="table, csv, or json"
    ),
    list_queries: bool = typer.Option(
        False, "--list", help="List available named queries and exit"
    ),
    show_sql: bool = typer.Option(
        False, "--show-sql", help="Print the assembled SQL and exit"
    ),
):
    """Run a named or ad-hoc read-only query against the local DB and print the rows."""
    if list_queries:
        for q in db_queries.list_queries():
            typer.echo(f"  {q.name:<20} {q.description}")
        return

    if output_format not in db_queries.PRINTERS:
        typer.echo(
            f"Unknown --format {output_format!r}; "
            f"expected one of {', '.join(db_queries.PRINTERS)}",
            err=True,
        )
        raise typer.Exit(code=2)

    if raw_sql and name:
        typer.echo("Pass either a query name or --sql, not both", err=True)
        raise typer.Exit(code=2)
    if raw_sql:
        base = raw_sql.strip().rstrip(";")
    elif name:
        try:
            base = db_queries.load_query(name).body
        except KeyError as exc:
            typer.echo(f"Unknown query {name!r}; run `query --list`", err=True)
            raise typer.Exit(code=2) from exc
    else:
        typer.echo("Pass a query name or --sql (or --list)", err=True)
        raise typer.Exit(code=2)

    statement = db_queries.build_sql(
        base,
        where=where,
        order_by=order_by,
        limit=limit if limit > 0 else None,
    )

    if show_sql:
        typer.echo(statement)
        return

    try:
        columns, rows = db_queries.run_readonly(statement)
    except db_queries.ReadOnlyViolation as exc:
        typer.echo("Rejected: query attempted to write (transaction is read-only)", err=True)
        raise typer.Exit(code=1) from exc
    except db_queries.QueryError as exc:
        typer.echo(f"SQL error: {str(exc).strip()}", err=True)
        raise typer.Exit(code=1) from exc

    db_queries.PRINTERS[output_format](columns, rows)
    if output_format == "table":
        suffix = " (limit reached)" if limit and len(rows) == limit else ""
        typer.echo(f"{len(rows)} row(s){suffix}", err=True)


__all__ = ["app"]
