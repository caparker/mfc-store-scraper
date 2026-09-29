"""Shopify Product model"""

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel

from src.shopify.types.models.metafield import Metafield
from src.shopify.types.models.options import ProductOption
from src.shopify.types.models.product_variant import ProductVariant


class Product(BaseModel):
    """
    Shopify product model
    docs: https://shopify.dev/docs/api/admin-graphql/latest/objects/Product
    """

    id: str
    options: Optional[List[ProductOption]] = None
    metafield: Optional[Metafield] = None
    variants: Optional[Dict[str, List[ProductVariant]]] = None


class ProductStatus(str, Enum):
    """
    The product status, which controls visibility across all sales channels.
    docs: https://shopify.dev/docs/api/admin-graphql/latest/mutations/productCreate?language=graphql
    """

    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"
    DRAFT = "DRAFT"
    # Local-only: the product no longer exists in Shopify. Never sent to
    # Shopify; rows in this state are skipped by every push. Set by pull-status.
    DELETED = "DELETED"
