"""Pydantic model for the `azure.customers` table"""

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, JsonValue


class CustomerModel(BaseModel):
    """Pydantic model for the `azure.customers` table"""

    id: int
    shopify_customer_id: Optional[str] = None
    email: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    default_address: JsonValue = None
    member_number: Optional[str] = None
    membership_status: Optional[str] = None
    member_since: Optional[date] = None
    membership_expires: Optional[date] = None
    notes: Optional[str] = None
    remote_updated_at: Optional[datetime] = None
    shopify_updated_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
