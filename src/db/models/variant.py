"""Pydantic model for the `azure.variants` table"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, JsonValue


class VariantModel(BaseModel):
    """Pydantic model for the `azure.variants` table"""

    id: int
    products_id: int
    code: str
    shopify_variant_id: Optional[str] = None
    shopify_inventory_item_id: Optional[str] = None
    size: str
    weight: JsonValue
    stock: int
    rewards_enabled: bool
    freight_handling_required: bool
    tags: JsonValue
    primary_category: Optional[int] = None
    favorites: int
    next_purchase_arrival: Optional[datetime] = None
    last_changed_fields: Optional[list[str]] = None
    last_seen_at: Optional[datetime] = None
    shopify_stock: Optional[int] = None
    shopify_updated_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
