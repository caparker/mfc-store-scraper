"""Pydantic models for the `azure.orders` and `azure.order_items` tables"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class OrderModel(BaseModel):
    """Pydantic model for the `azure.orders` table"""

    id: int
    shopify_order_id: str
    name: Optional[str] = None
    customers_id: Optional[int] = None
    shopify_customer_id: Optional[str] = None
    financial_status: Optional[str] = None
    fulfillment_status: Optional[str] = None
    ordered_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    note: Optional[str] = None
    last_pulled_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class OrderItemModel(BaseModel):
    """Pydantic model for the `azure.order_items` table"""

    id: int
    orders_id: int
    shopify_line_item_id: str
    shopify_variant_id: Optional[str] = None
    sku: Optional[str] = None
    packaging_code: Optional[str] = None
    title: Optional[str] = None
    variant_title: Optional[str] = None
    quantity: int
    unfulfilled_quantity: int
    created_at: datetime
    updated_at: datetime
