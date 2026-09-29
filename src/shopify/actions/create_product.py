"""Module for creating products in Shopify and saving data in the database"""

from psycopg import sql

from src.azure.azure_shopify_category_map import AZURE_SHOPIFY_CATEGORY_MAP
from src.db.models.product import ProductModel
from src.db.postgres import Database
from src.lib.logger import logger
from src.shopify.shopify import Shopify
from src.shopify.mutations import Mutations
from src.shopify.types.models.metafield import Metafield
from src.shopify.types.requests.product_create import (
    ProductCreateInput,
    ProductCreateResponse,
    OptionCreateInput,
)
from src.shopify.types.models.product import ProductStatus

class UnknownCategoryError(Exception):
    """Raised when azure category has no Shopify mapping."""


class ProductCreateError(Exception):
    """Generic product creation error"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)


def create_product(
    product: ProductModel,
    status: ProductStatus = ProductStatus.DRAFT,
) -> ProductModel:
    """Create a product in Shopify with the given status and an initial size option"""

    if product.shopify_product_id:
        return product

    (primary_category, secondary_category) = product.category.split(".")

    shopify_category = AZURE_SHOPIFY_CATEGORY_MAP.get(secondary_category)

    if shopify_category is None:
        raise UnknownCategoryError(
            f"No Shopify category mapping for '{secondary_category}'"
            )

    create_input = ProductCreateInput(
        category=shopify_category,
        descriptionHtml=product.description,
        handle=product.slug,
        productType=secondary_category,
        status=status,
        productOptions=[
            OptionCreateInput(
                name="Size",
            )
        ],
        tags=[
            primary_category,
            secondary_category,
            f"storage_{product.storage_climate}",
        ],
        title=product.name,
        vendor="Azure Standard",
        metafields=[Metafield(value=str(product.id))],
    )

    #logger.debug(f"create_input: {create_input.model_dump_json()}")

    shopify = Shopify()

    raw_product_create_response = shopify.query_file(
        Mutations.product_create, {"product": create_input.model_dump()}
    )

    #logger.debug(f"raw_product_create_response: {raw_product_create_response}")

    product_create_response = ProductCreateResponse.model_validate(
        raw_product_create_response
    )

    if len(product_create_response.errors):
        logger.error(product_create_response.model_dump_json())
        raise ProductCreateError(message=product_create_response.model_dump_json())

    if len(product_create_response.data.productCreate.userErrors):
        logger.error(product_create_response.model_dump_json())
        raise ProductCreateError(message=product_create_response.model_dump_json())

    db = Database()

    shopify_product_id = product_create_response.data.productCreate.product.id

    db.batch_execute(
        sql.SQL("""
            UPDATE azure.products
            SET
                shopify_product_id = %(shopify_product_id)s,
                shopify_status = %(shopify_status)s,
                shopify_updated_at = now()
            WHERE id = %(product_id)s;
        """),
        [
            {
                "product_id": product.id,
                "shopify_product_id": shopify_product_id,
                "shopify_status": status.value,
            }
        ],
    )

    product.shopify_product_id = shopify_product_id
    product.shopify_status = status

    return product
