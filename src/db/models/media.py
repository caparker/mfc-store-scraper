"""Pydantic model for the `azure.media` table"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class MediaModel(BaseModel):
    """Pydantic model for the `azure.media` table"""

    id: int
    packaging_code: str
    original_url: str
    file_name: str | None = None
    position: int = 0
    shopify_media_id: str | None = None
    shopify_status: str | None = None
    variant_media_set_at: Optional[datetime] = None
    error: str | None = None
    attempts: int = 0
    last_attempt_at: Optional[datetime] = None
    shopify_updated_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
