"""Pydantic models for the `azure.orders` and `azure.order_items` tables"""

from datetime import datetime
from decimal import Decimal
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
    currency: Optional[str] = None
    subtotal: Optional[Decimal] = None
    total_tax: Optional[Decimal] = None
    total_discounts: Optional[Decimal] = None
    total_shipping: Optional[Decimal] = None
    total: Optional[Decimal] = None
    net_payment: Optional[Decimal] = None
    total_refunded: Optional[Decimal] = None
    remote_updated_at: Optional[datetime] = None
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
    variants_id: Optional[int] = None
    title: Optional[str] = None
    variant_title: Optional[str] = None
    quantity: int
    current_quantity: Optional[int] = None
    unfulfilled_quantity: int
    cancelled_quantity: int = 0
    returned_quantity: int = 0
    status: Optional[str] = None
    original_unit_price: Optional[Decimal] = None
    discounted_unit_price: Optional[Decimal] = None
    discounted_total: Optional[Decimal] = None
    created_at: datetime
    updated_at: datetime
