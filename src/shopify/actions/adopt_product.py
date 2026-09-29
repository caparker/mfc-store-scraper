"""Action for linking a DB product to a Shopify product that already exists."""

from psycopg import sql

from src.db.models.product import ProductModel
from src.db.postgres import Database
from src.lib.logger import logger
from src.shopify.queries import Queries
from src.shopify.shopify import Shopify
from src.shopify.types.models.product import ProductStatus

from .create_product import ProductCreateError


def adopt_product(product: ProductModel) -> ProductModel:
    """Record the Shopify ids of a product that already exists under our handle.

    Used when productCreate fails because the handle is taken, which happens
    when the DB lost its shopify ids (rebuild, restore) but Shopify kept the
    product. The Shopify product must carry our internal.id metafield with the
    same Azure product id; otherwise this raises rather than link the wrong
    product.

    Variants are matched to azure.packaging by SKU ("AZ-<code>"), not by their
    internal.id metafield, because packaging ids are not stable across DB
    rebuilds. The variant metafield is refreshed on the next variant push.

    The product row is left dirty so the next update pushes the DB state.
    """
    shopify = Shopify()
    resp = shopify.query_file(Queries.product_by_handle, {"handle": product.slug})

    errors = resp.get("errors")
    if errors:
        raise ProductCreateError(message=f"{errors}")

    remote = resp["data"]["productByIdentifier"]
    if remote is None:
        raise ProductCreateError(
            message=f"Handle '{product.slug}' is in use but no product was found for it"
        )

    metafield = remote.get("metafield") or {}
    remote_azure_id = metafield.get("value")
    if remote_azure_id != str(product.id):
        raise ProductCreateError(
            message=(
                f"Handle '{product.slug}' belongs to {remote['id']} with "
                f"internal.id={remote_azure_id!r}, expected {product.id}; not adopting"
            )
        )

    shopify_product_id = remote["id"]
    status = ProductStatus(remote["status"])

    db = Database()
    db.batch_execute(
        sql.SQL(
            """
            UPDATE azure.products
            SET
                shopify_product_id = %(shopify_product_id)s
                , shopify_status = %(shopify_status)s
            WHERE id = %(product_id)s
            """
        ),
        [
            {
                "product_id": product.id,
                "shopify_product_id": shopify_product_id,
                "shopify_status": status.value,
            }
        ],
    )

    variants = remote.get("variants", {}).get("nodes", []) or []
    variant_rows = [
        {
            "product_id": product.id,
            "code": variant["sku"].removeprefix("AZ-"),
            "shopify_variant_id": variant["id"],
        }
        for variant in variants
        if variant.get("sku", "").startswith("AZ-")
    ]
    if variant_rows:
        db.batch_execute(
            sql.SQL(
                """
                UPDATE azure.packaging
                SET shopify_variant_id = %(shopify_variant_id)s
                WHERE products_id = %(product_id)s
                  AND code = %(code)s
                  AND shopify_variant_id IS NULL
                """
            ),
            variant_rows,
        )

    logger.info(
        f"Adopted {shopify_product_id} [{status.value}] for product {product.id} "
        f"({product.name}); linked {len(variant_rows)} of {len(variants)} variant(s) by SKU"
    )

    product.shopify_product_id = shopify_product_id
    product.shopify_status = status
    return product
