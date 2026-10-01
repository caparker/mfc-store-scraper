"""Module for managing Shopify GraphQL mutation files"""

import os
from pathlib import Path
from dataclasses import dataclass


@dataclass
class Mutations:
    """Class for managing GraphQL mutation files"""

    _query_directory = os.path.dirname(os.path.abspath(__file__))

    generate_staged_uploads = Path(
        os.path.join(_query_directory, "generate_staged_uploads.graphql")
    ).read_text(encoding="utf-8")

    product_create_media = Path(
        os.path.join(_query_directory, "product_create_media.graphql")
    ).read_text(encoding="utf-8")

    product_create = Path(
        os.path.join(_query_directory, "product_create.graphql")
    ).read_text(encoding="utf-8")

    product_update = Path(
        os.path.join(_query_directory, "product_update.graphql")
    ).read_text(encoding="utf-8")

    product_delete = Path(
        os.path.join(_query_directory, "product_delete.graphql")
    ).read_text(encoding="utf-8")

    product_variants_bulk_create = Path(
        os.path.join(_query_directory, "product_variants_bulk_create.graphql")
    ).read_text(encoding="utf-8")

    product_variants_bulk_update = Path(
        os.path.join(_query_directory, "product_variants_bulk_update.graphql")
    ).read_text(encoding="utf-8")

    product_option_update = Path(
        os.path.join(_query_directory, "product_option_update.graphql")
    ).read_text(encoding="utf-8")

    product_handle_update = Path(
        os.path.join(_query_directory, "product_handle_update.graphql")
    ).read_text(encoding="utf-8")


    inventory_set_on_hand = Path(
        os.path.join(_query_directory, "inventory_set_on_hand.graphql")
    ).read_text(encoding="utf-8")

    customer_create = Path(
        os.path.join(_query_directory, "customer_create.graphql")
    ).read_text(encoding="utf-8")

    metafields_set = Path(
        os.path.join(_query_directory, "metafields_set.graphql")
    ).read_text(encoding="utf-8")

    metafields_delete = Path(
        os.path.join(_query_directory, "metafields_delete.graphql")
    ).read_text(encoding="utf-8")

    tags_add = Path(
        os.path.join(_query_directory, "tags_add.graphql")
    ).read_text(encoding="utf-8")

    tags_remove = Path(
        os.path.join(_query_directory, "tags_remove.graphql")
    ).read_text(encoding="utf-8")
