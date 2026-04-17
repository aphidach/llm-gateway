"""
Provider quota state models.
"""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.common.time import ensure_utc


class ProviderQuotaConfig(BaseModel):
    """Runtime quota configuration derived from provider metadata."""

    provider_id: int = Field(..., ge=1)
    provider_name: Optional[str] = None
    provider_options: Optional[dict[str, Any]] = None


class ProviderDailyUsage(BaseModel):
    """Aggregated daily token usage for a provider."""

    provider_id: int = Field(..., ge=1)
    input_tokens_used: int = Field(0, ge=0)
    output_tokens_used: int = Field(0, ge=0)
    total_tokens_used: int = Field(0, ge=0)


class ProviderQuotaState(BaseModel):
    """Resolved runtime quota state for routing and admin inspection."""

    provider_id: int = Field(..., ge=1)
    provider_name: Optional[str] = None
    input_tokens_used: int = Field(0, ge=0)
    output_tokens_used: int = Field(0, ge=0)
    total_tokens_used: int = Field(0, ge=0)
    daily_token_budget: Optional[int] = Field(None, ge=1)
    soft_limit_tokens: Optional[int] = Field(None, ge=1)
    soft_limit_ratio: Optional[float] = Field(None, ge=0, le=1)
    status: str = Field("healthy", pattern="^(healthy|degraded|exhausted)$")
    in_cooldown: bool = False
    over_soft_limit: bool = False
    reset_at: datetime
    cooldown_until: Optional[datetime] = None
    last_quota_error_at: Optional[datetime] = None
    last_quota_error_message: Optional[str] = None
    last_quota_status_code: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)

    @field_validator("reset_at", "cooldown_until", "last_quota_error_at", mode="after")
    @classmethod
    def _ensure_utc(cls, value: Optional[datetime]) -> Optional[datetime]:
        return ensure_utc(value)
