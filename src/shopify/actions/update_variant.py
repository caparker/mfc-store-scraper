"""Shopify action to update variants from the local DB"""

from psycopg import rows, sql

from src.db.postgres import Database, MARKUP_PERCENTAGE
from src.db.models.packaging import PackagingModel
from src.db.models.price import PriceModel
from src.db.models.product import ProductModel
from src.lib.logger import logger
from src.shopify.actions import create_media
from src.shopify.shopify import Shopify
from src.shopify.mutations import Mutations
from src.shopify.types.models.metafield import Metafield
from src.shopify.types.requests.product_variants_bulk_input import (
    ProductVariantsBulkInput,
    ProductVariantInventoryPolicy,
    VariantOptionValueInput,
    ProductVariantsBulkUpdateResponse,
)

class ProductVariantUpdateError(Exception):
    """Generic product variant update error"""


def update_variant(packaging: PackagingModel) -> PackagingModel:
    """
    Used to update variants at any time updates are needed for prices, stock, etc.
    """

    if not packaging.shopify_variant_id:
        logger.debug(f"No shopify_variant_id set, cannot update [{packaging.id}]")
        return packaging

    db = Database()

    product = ProductModel.model_validate(
        db.fetchone(
            sql.SQL(
                """SELECT * FROM azure.products WHERE id = %(product_id)s LIMIT 1;"""
            ),
            {"product_id": packaging.products_id},
            rows.class_row(ProductModel),
        )
    )

    # packaging_media = MediaModel.model_validate(
    #     db.fetchone(
    #         sql.SQL(
    #             """SELECT * FROM azure.media WHERE packaging_code = %(packaging_code)s LIMIT 1;"""
    #         ),
    #         {"packaging_code": packaging.code},
    #         rows.class_row(MediaModel),
    #     )
    # )

    # if not packaging_media.shopify_media_id:
    #     packaging_media = create_media(packaging_media)

    # Get the most recent price set for variant
    packaging_price = PriceModel.model_validate(
        db.fetchone(
            sql.SQL(
                """SELECT * FROM azure.prices WHERE packaging_code = %(packaging_code)s ORDER BY created_at DESC LIMIT 1;"""
            ),
            {"packaging_code": packaging.code},
            rows.class_row(PriceModel),
        )
    )

    packaging_input = ProductVariantsBulkInput(
        id=packaging.shopify_variant_id,
        compareAtPrice=None,
        inventoryPolicy=ProductVariantInventoryPolicy.DENY,
        optionValues=[VariantOptionValueInput(name=packaging.size)],
        mediaId=None,
        price=f"{packaging_price.retail_dollars / (1 - (MARKUP_PERCENTAGE/100)):.2f}",
        metafields=[Metafield(value=str(packaging.id))],
    )

    shopify = Shopify()

    raw_variant_update_response = shopify.query_file(
        Mutations.product_variants_bulk_update,
        {
            "productId": product.shopify_product_id,
            "variants": [packaging_input.model_dump()],
            "namespace": "internal",
            "key": "id",
        },
    )

    logger.info(raw_variant_update_response)

    product_variants_bulk_response = ProductVariantsBulkUpdateResponse.model_validate(
        raw_variant_update_response
    )

    if len(product_variants_bulk_response.errors):
        logger.error(product_variants_bulk_response.model_dump_json())
        raise ProductVariantUpdateError()

    if len(product_variants_bulk_response.data.productVariantsBulkUpdate.userErrors):
        logger.error(product_variants_bulk_response.model_dump_json())
        raise ProductVariantUpdateError()

    db.batch_execute(
        sql.SQL("""
            UPDATE azure.packaging
            SET
                shopify_updated_at = now()
            WHERE id = %(packaging_id)s
        """),
        [{"packaging_id": packaging.id}],
    )

    return packaging
