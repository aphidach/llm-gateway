from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from app.common.time import utc_now
from app.domain.kv_store import KeyValueModel
from app.domain.quota import ProviderDailyUsage, ProviderQuotaConfig
from app.providers.base import ProviderResponse
from app.services.quota_service import ProviderQuotaService


class TestProviderQuotaService:
    @pytest.mark.asyncio
    async def test_get_provider_states_marks_soft_limit_as_degraded(self):
        log_repo = AsyncMock()
        kv_repo = AsyncMock()
        now = utc_now()
        log_repo.get_provider_daily_usage.return_value = [
            ProviderDailyUsage(
                provider_id=1,
                input_tokens_used=500,
                output_tokens_used=400,
                total_tokens_used=900,
            )
        ]
        kv_repo.get.return_value = None
        service = ProviderQuotaService(log_repo, kv_repo)

        states = await service.get_provider_states(
            [
                ProviderQuotaConfig(
                    provider_id=1,
                    provider_name="quota-provider",
                    provider_options={"quota": {"daily_token_budget": 1000}},
                )
            ],
            now=now,
        )

        state = states[1]
        assert state.status == "degraded"
        assert state.over_soft_limit is True
        assert state.daily_token_budget == 1000
        assert state.soft_limit_tokens == 800
        assert state.in_cooldown is False

    @pytest.mark.asyncio
    async def test_get_provider_states_marks_cooldown_as_exhausted(self):
        log_repo = AsyncMock()
        kv_repo = AsyncMock()
        now = utc_now()
        log_repo.get_provider_daily_usage.return_value = []
        kv_repo.get.return_value = KeyValueModel(
            key=ProviderQuotaService.cooldown_key(2),
            value=(
                '{"cooldown_until":"%s","last_quota_error_at":"%s",'
                '"last_quota_error_message":"quota exceeded","last_quota_status_code":429}'
                % ((now + timedelta(minutes=5)).isoformat(), now.isoformat())
            ),
            expires_at=now + timedelta(minutes=5),
            created_at=now,
            updated_at=now,
        )
        service = ProviderQuotaService(log_repo, kv_repo)

        states = await service.get_provider_states(
            [ProviderQuotaConfig(provider_id=2, provider_name="cooldown-provider")],
            now=now,
        )

        state = states[2]
        assert state.status == "exhausted"
        assert state.in_cooldown is True
        assert state.last_quota_status_code == 429
        assert state.last_quota_error_message == "quota exceeded"

    @pytest.mark.asyncio
    async def test_mark_quota_failure_persists_cooldown(self):
        log_repo = AsyncMock()
        kv_repo = AsyncMock()
        now = utc_now()
        service = ProviderQuotaService(log_repo, kv_repo)

        state = await service.mark_quota_failure(
            provider_id=3,
            provider_name="budgeted-provider",
            provider_options={"quota": {"cooldown_seconds": 120}},
            response=ProviderResponse(status_code=429, error="rate limit"),
            now=now,
        )

        assert state.status == "exhausted"
        assert state.in_cooldown is True
        assert state.last_quota_status_code == 429
        kv_repo.set.assert_awaited_once()
        ttl_seconds = kv_repo.set.await_args.kwargs["ttl_seconds"]
        assert ttl_seconds == 120

    def test_is_quota_failure_detects_status_and_payloads(self):
        assert ProviderQuotaService.is_quota_failure(
            ProviderResponse(status_code=429, error="too many requests")
        )
        assert ProviderQuotaService.is_quota_failure(
            ProviderResponse(
                status_code=400,
                body={"error": {"message": "insufficient_quota for today"}},
            )
        )
        assert not ProviderQuotaService.is_quota_failure(
            ProviderResponse(status_code=400, error="validation error")
        )
