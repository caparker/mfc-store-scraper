"""Module for managing Shopify GraphQL query files"""

import os
from pathlib import Path
from dataclasses import dataclass


@dataclass
class Queries:
    """Class for managing GraphQL query files"""

    _query_directory = os.path.dirname(os.path.abspath(__file__))

    check_file_status = Path(
        os.path.join(_query_directory, "check_file_status.graphql")
    ).read_text(encoding="utf-8")

    current_app_installation = Path(
        os.path.join(_query_directory, "current_app_installation.graphql")
    ).read_text(encoding="utf-8")

    get_product = Path(
        os.path.join(_query_directory, "get_product.graphql")
    ).read_text(encoding="utf-8")

    location_primary = Path(
        os.path.join(_query_directory, "location_primary.graphql")
    ).read_text(encoding="utf-8")

    inventory_items_by_variants = Path(
        os.path.join(_query_directory, "inventory_items_by_variants.graphql")
    ).read_text(encoding="utf-8")

    products_status_page = Path(
        os.path.join(_query_directory, "products_status_page.graphql")
    ).read_text(encoding="utf-8")

    products_by_ids = Path(
        os.path.join(_query_directory, "products_by_ids.graphql")
    ).read_text(encoding="utf-8")
