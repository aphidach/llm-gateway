import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_db
from datetime import timedelta
from app.config import get_settings
from app.main import app
from app.repositories.sqlalchemy.kv_store_repo import SQLAlchemyKVStoreRepository
from app.repositories.sqlalchemy.log_repo import SQLAlchemyLogRepository
from app.domain.log import RequestLogCreate
from app.common.time import utc_now


@pytest.mark.asyncio
async def test_admin_create_model_supports_quota_aware_strategy(db_session, monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "")
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    monkeypatch.setenv("KV_STORE_TYPE", "database")
    get_settings.cache_clear()

    app.dependency_overrides[get_db] = lambda: db_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/api/admin/models",
            json={
                "requested_model": "auto",
                "strategy": "quota_aware",
                "model_type": "chat",
                "is_active": True,
            },
        )

    assert resp.status_code == 201, resp.text
    assert resp.json()["strategy"] == "quota_aware"
    app.dependency_overrides = {}


@pytest.mark.asyncio
async def test_admin_match_model_respects_quota_cooldown(db_session, monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "")
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    monkeypatch.setenv("KV_STORE_TYPE", "database")
    get_settings.cache_clear()

    app.dependency_overrides[get_db] = lambda: db_session
    kv_repo = SQLAlchemyKVStoreRepository(db_session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        provider1 = await ac.post(
            "/api/admin/providers",
            json={
                "name": "quota-provider-1",
                "base_url": "https://provider1.example.com",
                "protocol": "openai",
                "api_type": "chat",
                "is_active": True,
            },
        )
        provider2 = await ac.post(
            "/api/admin/providers",
            json={
                "name": "quota-provider-2",
                "base_url": "https://provider2.example.com",
                "protocol": "openai",
                "api_type": "chat",
                "is_active": True,
            },
        )
        assert provider1.status_code == 201, provider1.text
        assert provider2.status_code == 201, provider2.text
        provider1_id = provider1.json()["id"]
        provider2_id = provider2.json()["id"]

        model_resp = await ac.post(
            "/api/admin/models",
            json={
                "requested_model": "auto",
                "strategy": "quota_aware",
                "model_type": "chat",
                "is_active": True,
            },
        )
        assert model_resp.status_code == 201, model_resp.text

        for provider_id, target_model_name in (
            (provider1_id, "model-a"),
            (provider2_id, "model-b"),
        ):
            mapping_resp = await ac.post(
                "/api/admin/model-providers",
                json={
                    "requested_model": "auto",
                    "provider_id": provider_id,
                    "target_model_name": target_model_name,
                    "priority": 0 if provider_id == provider1_id else 1,
                    "billing_mode": "token_flat",
                    "input_price": 1,
                    "output_price": 2,
                },
            )
            assert mapping_resp.status_code == 201, mapping_resp.text

        now = utc_now().replace(microsecond=0)
        await kv_repo.set(
            "quota_cooldown:provider:%s" % provider1_id,
            '{"cooldown_until":"%s","last_quota_error_at":"%s","last_quota_error_message":"quota exceeded","last_quota_status_code":429}'
            % (
                (now + timedelta(minutes=10)).isoformat(),
                now.isoformat(),
            ),
            ttl_seconds=900,
        )

        match_resp = await ac.post(
            "/api/admin/models/auto/match",
            json={"input_tokens": 256},
        )

    assert match_resp.status_code == 200, match_resp.text
    items = match_resp.json()
    assert [item["provider_id"] for item in items] == [provider2_id]
    app.dependency_overrides = {}


@pytest.mark.asyncio
async def test_admin_provider_quota_status_reports_daily_usage(db_session, monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "")
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    monkeypatch.setenv("KV_STORE_TYPE", "database")
    get_settings.cache_clear()

    app.dependency_overrides[get_db] = lambda: db_session
    log_repo = SQLAlchemyLogRepository(db_session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        provider_resp = await ac.post(
            "/api/admin/providers",
            json={
                "name": "quota-status-provider",
                "base_url": "https://quota-status.example.com",
                "protocol": "openai",
                "api_type": "chat",
                "provider_options": {"quota": {"daily_token_budget": 1000}},
                "is_active": True,
            },
        )
        assert provider_resp.status_code == 201, provider_resp.text
        provider_id = provider_resp.json()["id"]

        await log_repo.create(
            RequestLogCreate(
                request_time=utc_now(),
                requested_model="auto",
                target_model="model-a",
                provider_id=provider_id,
                provider_name="quota-status-provider",
                input_tokens=500,
                output_tokens=350,
                response_status=200,
            )
        )

        quota_resp = await ac.get("/api/admin/providers/quota-status")

    assert quota_resp.status_code == 200, quota_resp.text
    item = next(row for row in quota_resp.json() if row["provider_id"] == provider_id)
    assert item["total_tokens_used"] == 850
    assert item["status"] == "degraded"
    assert item["daily_token_budget"] == 1000
    app.dependency_overrides = {}
