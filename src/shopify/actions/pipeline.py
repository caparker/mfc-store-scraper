"""End-to-end pipeline: scrape -> sync products -> sync variants -> sync media
-> sync stock -> pull customers -> sync customers -> pull orders -> dump."""

from src.azure.cli.get_products_from_azure import get_products_from_azure
from .dump_database import dump_database
from .add_products import add_products
from .update_products import update_products
from .update_variants import update_variants
from .update_stock import update_stock
from .sync_media import sync_media
from .pull_customers import pull_customers
from .update_customers import update_customers
from .pull_orders import pull_orders
from src.lib.logger import logger


def _section(title: str) -> None:
    logger.info("")
    logger.info(f"== {title} ==")


def run_pipeline() -> None:
    """Run the full sync pipeline. Fail-fast: any exception aborts."""
    _section("Scraping Azure")
    get_products_from_azure()

    _section("Creating new products in Shopify")
    add_products()

    _section("Updating dirty products in Shopify")
    update_products()

    _section("Updating dirty variants in Shopify")
    update_variants()

    _section("Syncing product images to Shopify")
    sync_media()

    _section("Updating stock in Shopify")
    update_stock()

    _section("Pulling customers from Shopify")
    pull_customers()

    _section("Pushing membership to Shopify")
    update_customers()

    _section("Pulling open orders from Shopify")
    pull_orders()

    _section("Dumping database")
    dump_database()

    logger.success("Pipeline complete")
