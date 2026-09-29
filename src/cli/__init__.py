import typer

from psycopg import sql, rows
from src.db.postgres import Database
from src.shopify.actions import (
    add_products,
    update_products,
    update_variants,
    dump_database,
    run_pipeline,
    set_product_status,
    pull_product_status,
)
from src.cli.actions.status import sync_status, sync_samples
from src.shopify.shopify import Shopify, ShopifyConnectionError
from src.shopify.types.models.product import ProductStatus


app = typer.Typer()


@app.command()
def check_connection():
    """Verify Shopify credentials and reachability."""
    shop = Shopify()
    try:
        info = shop.check_connection()
        app_name = info.get("currentAppInstallation", {}).get("app", {}).get("title", "?")
        typer.echo(f"✓ Connected to {shop.shop_domain} as {app_name}")
    except ShopifyConnectionError as e:
        typer.echo(f"✗ Connection failed: {e}", err=True)
        raise typer.Exit(code=1)


@app.command()
def run():
    """Run the full pipeline: scrape → sync products → sync variants → dump."""
    run_pipeline()


@app.command()
def dump_db(
    output_dir: str = typer.Option(
        "./dumps", "--output-dir", help="Directory to write the dump to"
    ),
):
    """Dump the Postgres database to a timestamped SQL file."""
    dump_database(output_dir=output_dir)


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
    """Push dirty azure.packaging rows to Shopify (variant-level price)."""
    update_variants(
        packaging_code=packaging_code,
        product_id=product_id,
        max_workers=max_workers,
        limit=limit,
    )

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
        False, "--dry-run", help="Report differences without writing to the DB"
    ),
):
    """Reconcile local product status with Shopify: pull statuses, mark deleted, report orphans."""
    counts = pull_product_status(dry_run=dry_run)
    typer.echo(f"  status changes:  {counts['changed']:>6}")
    typer.echo(f"  deleted:         {counts['deleted']:>6}")
    typer.echo(f"  orphans:         {counts['orphans']:>6}")


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
    total = sum(v for k, v in counts.items() if k != "products_deleted")

    typer.echo("Sync status:")
    typer.echo(f"  products new:    {counts['products_new']:>6}")
    typer.echo(f"  products dirty:  {counts['products_dirty']:>6}")
    typer.echo(f"  variants new:    {counts['variants_new']:>6}")
    typer.echo(f"  variants dirty:  {counts['variants_dirty']:>6}")
    typer.echo(f"  total pending:   {total:>6}")
    typer.echo(f"  products deleted in Shopify (not synced): {counts['products_deleted']}")

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


__all__ = ["app"]
