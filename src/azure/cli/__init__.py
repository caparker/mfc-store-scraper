import typer

from src.shopify.actions.purchase_list import get_purchase_list, commit_supplier_order

from .get_products_from_azure import get_products_from_azure

app = typer.Typer()


@app.command()
def scrape(
    limit: int = typer.Option(
        None, "--limit", "-l", help="Limit total products scraped (dev use)"
    ),
):
    """Scrape products from Azure Standard into the DB."""
    get_products_from_azure(limit=limit)


@app.command()
def quick_order(
    limit: int = typer.Option(
        None, "--limit", help="Only add the first N items (use for a first careful run)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Fill the form for each item but do not click Quick-Add"
    ),
    commit: bool = typer.Option(
        True, "--commit/--no-commit",
        help="Record the items that were added as a supplier order (default) "
             "or leave the list as is",
    ),
    notes: str = typer.Option(None, "--notes", help="Notes to store on the supplier order"),
):
    """Add the purchase list to the Azure cart via the site's quick-add form, in a visible browser.

    You log in by hand; the tool never checks out. Items that were added are
    recorded as a supplier order so they drop off the purchase list.
    """
    # Imported here so the rest of the CLI works without a browser installed.
    from src.azure.quick_order import run_quick_order  # pylint: disable=import-outside-toplevel

    items = [
        {"code": i["packaging_code"], "quantity": int(i["outstanding"]), "name": i["product_name"]}
        for i in get_purchase_list()
        if i["packaging_code"]
    ]
    if limit is not None:
        items = items[:limit]
    if not items:
        typer.echo("Nothing outstanding for Azure; run pull-orders first?")
        raise typer.Exit()

    typer.echo(f"{len(items)} item(s) to add" + (" (dry run)" if dry_run else ""))
    for i in items:
        typer.echo(f"  {i['code']:<12} x{i['quantity']:<4} {i['name']}")

    def wait_for_user(message: str) -> None:
        typer.echo("")
        typer.prompt(message, default="", show_default=False, prompt_suffix=" ")

    def on_done(added: list[dict], failed: list[dict]) -> None:
        typer.echo(f"\nAdded {len(added)}, failed {len(failed)}")
        for f in failed:
            typer.echo(f"  FAILED {f['code']:<12} x{f['quantity']:<4} {f['name']}: {f['detail']}")
        if commit and not dry_run and added:
            counts = commit_supplier_order(
                supplier="azure", notes=notes, packaging_codes=[a["code"] for a in added]
            )
            typer.echo(
                f"Recorded supplier order {counts['supplier_order_id']}: "
                f"{counts['line_items']} variant(s), {counts['units']} unit(s), "
                f"${counts['total_cost']:.2f}"
            )
        elif added and not dry_run:
            typer.echo("Not recorded (--no-commit); run `purchase-list --commit` when placed")

    run_quick_order(items, wait_for_user=wait_for_user, dry_run=dry_run, on_done=on_done)


__all__ = ["app"]
