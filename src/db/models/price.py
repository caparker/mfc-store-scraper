"""Pydantic model for the `azure.prices` table"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class PriceModel(BaseModel):
    """Pydantic model for the `azure.prices` table"""

    id: int
    variants_id: int
    retail_dollars: Optional[float] = None
    retail_unit: Optional[str] = None
    wholesale_dollars: Optional[float] = None
    wholesale_unit: Optional[str] = None
    created_at: datetime
