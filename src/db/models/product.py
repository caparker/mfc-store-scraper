"""Pydantic model for the `azure.products` table"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, JsonValue

from src.shopify.types.models.product import ProductStatus


class ProductModel(BaseModel):
    """Pydantic model for the `azure.products` table"""

    id: int
    shopify_product_id: str | None
    name: str
    short_description: str | None
    description: str
    slug: str
    storage_climate: str
    unshippable_regions: JsonValue
    brand: JsonValue
    substitutions: JsonValue
    category: str
    shopify_status: ProductStatus = ProductStatus.DRAFT
    shopify_updated_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
