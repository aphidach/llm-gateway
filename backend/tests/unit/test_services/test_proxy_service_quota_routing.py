from unittest.mock import AsyncMock, patch

import pytest

from app.common.time import utc_now
from app.domain.model import ModelMapping
from app.domain.quota import ProviderQuotaState
from app.providers.base import ProviderResponse
from app.rules.models import CandidateProvider
from app.services.proxy_service import ProxyService


def _candidate(
    provider_id: int,
    provider_mapping_id: int,
    target_model: str,
) -> CandidateProvider:
    return CandidateProvider(
        provider_id=provider_id,
        provider_mapping_id=provider_mapping_id,
        provider_name=f"provider-{provider_id}",
        base_url=f"https://provider-{provider_id}.example.com",
        protocol="openai",
        api_key=f"key-{provider_id}",
        target_model=target_model,
        priority=0 if provider_id == 1 else 1,
        weight=1,
    )


@pytest.mark.asyncio
async def test_process_request_quota_failure_skips_sibling_mappings_and_logs_routing():
    now = utc_now()
    model_mapping = ModelMapping(
        requested_model="auto",
        strategy="quota_aware",
        matching_rules=None,
        capabilities=None,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    candidates = [
        _candidate(1, 101, "model-a"),
        _candidate(1, 102, "model-b"),
        _candidate(2, 201, "model-c"),
    ]

    log_repo = AsyncMock()
    quota_service = AsyncMock()
    quota_service.get_provider_states.return_value = {
        1: ProviderQuotaState(provider_id=1, status="healthy", reset_at=now),
        2: ProviderQuotaState(provider_id=2, status="healthy", reset_at=now),
    }
    quota_service.is_quota_failure = lambda response: response.status_code == 429
    quota_service.mark_quota_failure.return_value = ProviderQuotaState(
        provider_id=1,
        status="exhausted",
        in_cooldown=True,
        reset_at=now,
    )

    service = ProxyService(
        model_repo=AsyncMock(),
        provider_repo=AsyncMock(),
        log_repo=log_repo,
        quota_service=quota_service,
    )
    service._resolve_candidates = AsyncMock(  # type: ignore[method-assign]
        return_value=(model_mapping, candidates, 128, "openai", {})
    )

    called_targets: list[str] = []

    async def forward(*, target_model: str, **kwargs):
        called_targets.append(target_model)
        if target_model == "model-a":
            return ProviderResponse(status_code=429, error="insufficient_quota")
        return ProviderResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            body={"id": "ok", "usage": {"completion_tokens": 4}},
        )

    fake_client = AsyncMock()
    fake_client.forward = AsyncMock(side_effect=forward)

    with patch("app.services.proxy_service.get_provider_client", return_value=fake_client):
        with patch(
            "app.services.proxy_service.convert_request_for_supplier",
            return_value=("/v1/chat/completions", {"model": "model-a", "messages": []}),
        ):
            response, _ = await service.process_request(
                api_key_id=1,
                api_key_name="auto-key",
                request_protocol="openai",
                path="/v1/chat/completions",
                request_url="/v1/chat/completions",
                method="POST",
                headers={},
                body={"model": "auto", "messages": []},
            )

    assert response.status_code == 200
    assert called_targets == ["model-a", "model-c"]
    final_log = log_repo.create.await_args_list[-1].args[0]
    assert final_log.requested_model == "auto"
    assert final_log.target_model == "model-c"
    assert final_log.provider_name == "provider-2"
    assert final_log.routing_details["strategy"] == "quota_aware"
    assert final_log.routing_details["selected_target_model"] == "model-c"
    assert final_log.routing_details["failures"][0]["reason"] == "quota_failure"
