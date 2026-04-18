"""
Provider quota tracking and quota-failure detection.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from app.common.time import ensure_utc, utc_now
from app.config import get_settings
from app.domain.quota import (
    ProviderDailyUsage,
    ProviderQuotaConfig,
    ProviderQuotaState,
)
from app.providers.base import ProviderResponse
from app.repositories.kv_store_repo import KVStoreRepository
from app.repositories.log_repo import LogRepository

_COOLDOWN_KEY_PREFIX = "quota_cooldown:provider:"
_QUOTA_ERROR_PATTERNS = (
    "insufficient_quota",
    "quota exceeded",
    "quota_exceeded",
    "exceeded your current quota",
    "quota has been exceeded",
    "daily limit",
    "reached daily limit",
    "model reached daily limit",
    "daily token limit",
    "daily quota",
    "usage limit",
    "billing hard limit",
)


@dataclass
class _QuotaOptions:
    daily_token_budget: Optional[int]
    soft_limit_ratio: float
    cooldown_seconds: int


class ProviderQuotaService:
    """Runtime quota state backed by logs + KV cooldown markers."""

    def __init__(
        self,
        log_repo: LogRepository,
        kv_repo: KVStoreRepository,
    ):
        self.log_repo = log_repo
        self.kv_repo = kv_repo
        self.settings = get_settings()

    @staticmethod
    def cooldown_key(provider_id: int) -> str:
        return f"{_COOLDOWN_KEY_PREFIX}{provider_id}"

    @staticmethod
    def _start_of_day(now: datetime) -> datetime:
        now = ensure_utc(now) or utc_now()
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    @classmethod
    def _next_reset_at(cls, now: datetime) -> datetime:
        return cls._start_of_day(now) + timedelta(days=1)

    def _extract_quota_options(
        self, provider_options: Optional[dict[str, Any]]
    ) -> _QuotaOptions:
        quota_options = (
            provider_options.get("quota")
            if isinstance(provider_options, dict)
            and isinstance(provider_options.get("quota"), dict)
            else {}
        )
        raw_budget = None
        raw_soft_ratio = None
        raw_cooldown = None
        if isinstance(provider_options, dict):
            raw_budget = provider_options.get("daily_token_budget")
            raw_soft_ratio = provider_options.get("soft_limit_ratio")
            raw_cooldown = provider_options.get("quota_cooldown_seconds")
        if isinstance(quota_options, dict):
            raw_budget = quota_options.get("daily_token_budget", raw_budget)
            raw_soft_ratio = quota_options.get("soft_limit_ratio", raw_soft_ratio)
            raw_cooldown = quota_options.get("cooldown_seconds", raw_cooldown)

        budget = self._coerce_positive_int(raw_budget)
        soft_limit_ratio = self._coerce_ratio(
            raw_soft_ratio, self.settings.QUOTA_AWARE_SOFT_LIMIT_RATIO
        )
        cooldown_seconds = (
            self._coerce_positive_int(raw_cooldown)
            or self.settings.QUOTA_AWARE_COOLDOWN_SECONDS
        )
        return _QuotaOptions(
            daily_token_budget=budget,
            soft_limit_ratio=soft_limit_ratio,
            cooldown_seconds=cooldown_seconds,
        )

    @staticmethod
    def _coerce_positive_int(value: Any) -> Optional[int]:
        if value in (None, ""):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _coerce_ratio(value: Any, default: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        if parsed <= 0:
            return default
        if parsed > 1:
            return 1.0
        return parsed

    @staticmethod
    def _extract_quota_error_text(response: ProviderResponse) -> str:
        parts: list[str] = []
        if response.error:
            parts.append(str(response.error))
        body = response.body
        if isinstance(body, bytes):
            try:
                body = body.decode("utf-8", errors="ignore")
            except Exception:
                body = None
        if isinstance(body, str):
            parts.append(body)
        elif isinstance(body, dict):
            try:
                parts.append(json.dumps(body, ensure_ascii=False))
            except Exception:
                parts.append(str(body))
        return " ".join(part for part in parts if part).lower()

    @classmethod
    def is_quota_failure(cls, response: ProviderResponse) -> bool:
        if response.status_code == 429:
            return True
        haystack = cls._extract_quota_error_text(response)
        return any(pattern in haystack for pattern in _QUOTA_ERROR_PATTERNS)

    async def get_provider_states(
        self,
        provider_configs: list[ProviderQuotaConfig],
        *,
        now: Optional[datetime] = None,
    ) -> dict[int, ProviderQuotaState]:
        resolved_now = ensure_utc(now) or utc_now()
        if not provider_configs:
            return {}

        provider_ids = [config.provider_id for config in provider_configs]
        usage_rows = await self.log_repo.get_provider_daily_usage(
            provider_ids=provider_ids,
            start_time=self._start_of_day(resolved_now),
            end_time=self._next_reset_at(resolved_now),
        )
        usage_map = {row.provider_id: row for row in usage_rows}

        states: dict[int, ProviderQuotaState] = {}
        for config in provider_configs:
            usage = usage_map.get(config.provider_id) or ProviderDailyUsage(
                provider_id=config.provider_id
            )
            states[config.provider_id] = await self._build_state(
                config=config,
                usage=usage,
                now=resolved_now,
            )
        return states

    async def _build_state(
        self,
        *,
        config: ProviderQuotaConfig,
        usage: ProviderDailyUsage,
        now: datetime,
    ) -> ProviderQuotaState:
        options = self._extract_quota_options(config.provider_options)
        soft_limit_tokens = (
            max(1, math.floor(options.daily_token_budget * options.soft_limit_ratio))
            if options.daily_token_budget
            else None
        )
        cooldown_record = await self._get_cooldown_record(config.provider_id)
        cooldown_until = cooldown_record.get("cooldown_until")
        in_cooldown = bool(
            cooldown_until is not None and cooldown_until > (ensure_utc(now) or utc_now())
        )
        exhausted_by_budget = bool(
            options.daily_token_budget
            and usage.total_tokens_used >= options.daily_token_budget
        )
        over_soft_limit = bool(
            soft_limit_tokens and usage.total_tokens_used >= soft_limit_tokens
        )

        status = "healthy"
        if exhausted_by_budget or in_cooldown:
            status = "exhausted"
        elif over_soft_limit:
            status = "degraded"

        return ProviderQuotaState(
            provider_id=config.provider_id,
            provider_name=config.provider_name,
            input_tokens_used=usage.input_tokens_used,
            output_tokens_used=usage.output_tokens_used,
            total_tokens_used=usage.total_tokens_used,
            daily_token_budget=options.daily_token_budget,
            soft_limit_tokens=soft_limit_tokens,
            soft_limit_ratio=options.soft_limit_ratio
            if options.daily_token_budget
            else None,
            status=status,
            in_cooldown=in_cooldown,
            over_soft_limit=over_soft_limit,
            reset_at=self._next_reset_at(now),
            cooldown_until=cooldown_until,
            last_quota_error_at=cooldown_record.get("last_quota_error_at"),
            last_quota_error_message=cooldown_record.get("last_quota_error_message"),
            last_quota_status_code=cooldown_record.get("last_quota_status_code"),
        )

    async def mark_quota_failure(
        self,
        *,
        provider_id: int,
        provider_name: Optional[str],
        provider_options: Optional[dict[str, Any]],
        response: ProviderResponse,
        current_state: Optional[ProviderQuotaState] = None,
        now: Optional[datetime] = None,
    ) -> ProviderQuotaState:
        resolved_now = ensure_utc(now) or utc_now()
        options = self._extract_quota_options(provider_options)
        cooldown_until = resolved_now + timedelta(seconds=options.cooldown_seconds)
        payload = {
            "provider_id": provider_id,
            "provider_name": provider_name,
            "cooldown_until": cooldown_until.isoformat(),
            "last_quota_error_at": resolved_now.isoformat(),
            "last_quota_error_message": response.error or "quota failure",
            "last_quota_status_code": response.status_code,
        }
        await self.kv_repo.set(
            self.cooldown_key(provider_id),
            json.dumps(payload, ensure_ascii=False),
            ttl_seconds=options.cooldown_seconds,
        )

        base = current_state or ProviderQuotaState(
            provider_id=provider_id,
            provider_name=provider_name,
            reset_at=self._next_reset_at(resolved_now),
        )
        return base.model_copy(
            update={
                "provider_name": provider_name or base.provider_name,
                "status": "exhausted",
                "in_cooldown": True,
                "cooldown_until": cooldown_until,
                "last_quota_error_at": resolved_now,
                "last_quota_error_message": response.error or "quota failure",
                "last_quota_status_code": response.status_code,
            }
        )

    async def _get_cooldown_record(self, provider_id: int) -> dict[str, Any]:
        cached = await self.kv_repo.get(self.cooldown_key(provider_id))
        if cached is None:
            return {}
        try:
            raw = json.loads(cached.value)
        except json.JSONDecodeError:
            return {}
        cooldown_until = ensure_utc(
            self._parse_iso_datetime(raw.get("cooldown_until"))
        )
        last_error_at = ensure_utc(
            self._parse_iso_datetime(raw.get("last_quota_error_at"))
        )
        return {
            "cooldown_until": cooldown_until,
            "last_quota_error_at": last_error_at,
            "last_quota_error_message": raw.get("last_quota_error_message"),
            "last_quota_status_code": raw.get("last_quota_status_code"),
        }

    @staticmethod
    def _parse_iso_datetime(value: Any) -> Optional[datetime]:
        if not isinstance(value, str) or not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
