"""Shopify action to create variants for a given product from the local DB"""

from typing import List

from psycopg import rows, sql

from src.db.postgres import Database, MARKUP_PERCENTAGE
from src.db.models.variant import VariantModel
from src.db.models.price import PriceModel
from src.db.models.product import ProductModel
from src.lib.logger import logger
from src.shopify.shopify import Shopify
from src.shopify.mutations import Mutations
from src.shopify.types.models.metafield import Metafield
from src.shopify.types.requests.product_variants_bulk_input import (
    ProductVariantsBulkInput,
    ProductVariantInventoryPolicy,
    InventoryItemInput,
    VariantOptionValueInput,
    ProductVariantsBulkCreateResponse,
)


class ProductVariantCreateError(Exception):
    """Generic product variant creation error"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)


def create_variants_for_product(product: ProductModel) -> List[VariantModel]:
    """
    Used when a product already exists and we're adding new sizes/variants for the product
    Additionally, used during the product create process to create initial variants
    """

    db = Database()
    product_variants: List[VariantModel] = db.fetchall(
        sql.SQL("""SELECT * FROM azure.variants WHERE products_id = %(product_id)s"""),
        {"product_id": product.id},
        rows.class_row(VariantModel),
    )

    sorted_variants: List[VariantModel] = sorted(
        product_variants, key=lambda variant: variant.weight["net"]
    )

    variant_inputs: List[ProductVariantsBulkInput] = []

    for variant in sorted_variants:
        if variant.shopify_variant_id:
            logger.debug(f"Variant already exists, skipping [{variant.model_dump_json()}]")
            continue

        variant_price = PriceModel.model_validate(
            db.fetchone(
                sql.SQL(
                    """SELECT * FROM azure.current_prices WHERE variants_id = %(variants_id)s"""
                ),
                {"variants_id": variant.id},
                rows.class_row(PriceModel),
            )
        )

        if not variant_price.retail_dollars:
            logger.debug("Variant has no price, skipping...")
            continue

        cost = (
            f"{round(variant_price.wholesale_dollars, 2):.2f}"
            if variant_price.wholesale_dollars
            else None
        )

        variant_input = ProductVariantsBulkInput(
            compareAtPrice=None,
            inventoryItem=InventoryItemInput(cost=cost, sku=f"AZ-{variant.code}"),
            inventoryPolicy=ProductVariantInventoryPolicy.DENY,
            optionValues=[VariantOptionValueInput(name=variant.size)],
            mediaId=None,  # set by sync-media once the variant exists
            price=f"{round(variant_price.retail_dollars / (1 - (MARKUP_PERCENTAGE/100)), 2):.2f}",
            metafields=[Metafield(value=str(variant.id))],
        )

        # Remove the ID field before creation
        del variant_input.id

        variant_inputs.append(variant_input.model_dump())

    if not variant_inputs:
        logger.debug(f"No variants to create for product {product.id}")
        return sorted_variants

    shopify = Shopify()

    raw_variant_create_response = shopify.query_file(
        Mutations.product_variants_bulk_create,
        {
            "productId": product.shopify_product_id,
            "variants": variant_inputs,
            "namespace": "internal",
            "key": "id",
        },
    )

    ##logger.debug(raw_variant_create_response)

    product_variants_bulk_response = ProductVariantsBulkCreateResponse.model_validate(
        raw_variant_create_response
    )

    if len(product_variants_bulk_response.errors):
        logger.error(product_variants_bulk_response.model_dump_json())
        raise ProductVariantCreateError(
            message=product_variants_bulk_response.model_dump_json()
        )

    if len(product_variants_bulk_response.data.productVariantsBulkCreate.userErrors):
        logger.error(product_variants_bulk_response.model_dump_json())
        raise ProductVariantCreateError(
            message=product_variants_bulk_response.model_dump_json()
        )

    for (
        variant
    ) in product_variants_bulk_response.data.productVariantsBulkCreate.productVariants:
        db.batch_execute(
            sql.SQL("""
                UPDATE azure.variants
                SET
                    shopify_variant_id = %(shopify_variant_id)s,
                    shopify_updated_at = now()
                WHERE id = %(variants_id)s
            """),
            [
                {
                    "variants_id": int(variant.metafield.value),
                    "shopify_variant_id": variant.id,
                }
            ],
        )

    return sorted_variants
