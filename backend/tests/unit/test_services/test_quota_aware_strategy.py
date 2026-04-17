import pytest

from app.common.time import utc_now
from app.domain.quota import ProviderQuotaState
from app.rules.models import CandidateProvider
from app.services.strategy import QuotaAwareStrategy


def _candidate(
    provider_id: int,
    provider_name: str,
    target_model: str,
    *,
    priority: int,
    input_price: float | None = None,
) -> CandidateProvider:
    return CandidateProvider(
        provider_id=provider_id,
        provider_name=provider_name,
        base_url=f"https://{provider_name}.example.com",
        protocol="openai",
        api_key=f"key-{provider_id}",
        target_model=target_model,
        priority=priority,
        input_price=input_price,
        output_price=1.0 if input_price is not None else None,
        billing_mode="token_flat" if input_price is not None else None,
        model_input_price=10.0,
        model_output_price=20.0,
    )


def _state(
    provider_id: int,
    *,
    status: str = "healthy",
    in_cooldown: bool = False,
    over_soft_limit: bool = False,
) -> ProviderQuotaState:
    return ProviderQuotaState(
        provider_id=provider_id,
        status=status,
        in_cooldown=in_cooldown,
        over_soft_limit=over_soft_limit,
        reset_at=utc_now(),
    )


class TestQuotaAwareStrategy:
    def setup_method(self):
        self.strategy = QuotaAwareStrategy()

    @pytest.mark.asyncio
    async def test_select_prefers_healthy_priority_provider(self):
        candidates = [
            _candidate(1, "p1", "model-a", priority=0),
            _candidate(2, "p2", "model-b", priority=1),
        ]

        selected = await self.strategy.select(candidates, "auto")
        assert selected is not None
        assert selected.provider_id == 1

    @pytest.mark.asyncio
    async def test_select_skips_provider_in_cooldown(self):
        candidates = [
            _candidate(1, "p1", "model-a", priority=0),
            _candidate(2, "p2", "model-b", priority=1),
        ]
        quota_state_map = {
            1: _state(1, status="exhausted", in_cooldown=True),
            2: _state(2),
        }

        selected = await self.strategy.select(
            candidates, "auto", quota_state_map=quota_state_map
        )
        assert selected is not None
        assert selected.provider_id == 2

    @pytest.mark.asyncio
    async def test_select_deprioritizes_soft_limit_provider(self):
        candidates = [
            _candidate(1, "p1", "model-a", priority=0),
            _candidate(2, "p2", "model-b", priority=1),
        ]
        quota_state_map = {
            1: _state(1, status="degraded", over_soft_limit=True),
            2: _state(2),
        }

        selected = await self.strategy.select(
            candidates, "auto", quota_state_map=quota_state_map
        )
        assert selected is not None
        assert selected.provider_id == 2

    @pytest.mark.asyncio
    async def test_select_tie_breaks_by_cost_then_round_robin(self):
        candidates = [
            _candidate(1, "cheap", "model-a", priority=0, input_price=1.0),
            _candidate(2, "expensive", "model-b", priority=0, input_price=2.0),
        ]

        selected = await self.strategy.select(candidates, "auto", input_tokens=1000)
        assert selected is not None
        assert selected.provider_id == 1

        tied = [
            _candidate(3, "tie-a", "model-c", priority=0, input_price=1.0),
            _candidate(4, "tie-b", "model-d", priority=0, input_price=1.0),
        ]
        first = await self.strategy.select(tied, "auto", input_tokens=1000)
        second = await self.strategy.select(tied, "auto", input_tokens=1000)
        assert first is not None and second is not None
        assert {first.provider_id, second.provider_id} == {3, 4}
