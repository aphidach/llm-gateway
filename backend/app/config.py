"""
Configuration Management Module

Configures application parameters via environment variables or .env file.
Supports SQLite (default) and PostgreSQL databases.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parents[2]
ENV_FILES = (
    ROOT_DIR / ".env",
    ROOT_DIR / "backend" / ".env",
)


class Settings(BaseSettings):
    """
    Application Configuration Class

    All configuration items can be overridden by environment variables, with names matching fields (uppercase).
    """

    # Application Config
    APP_NAME: str = "LLM Gateway"
    DEBUG: bool = False

    # Database Config
    # Supports "sqlite" or "postgresql"
    DATABASE_TYPE: Literal["sqlite", "postgresql"] = "sqlite"
    # SQLite default database path, PostgreSQL requires full connection string
    DATABASE_URL: str = "sqlite+aiosqlite:///./llm_gateway.db"

    # Retry Config
    # Max retries on same provider (triggered when status code >= 500)
    RETRY_MAX_ATTEMPTS: int = 3
    # Retry interval (ms)
    RETRY_DELAY_MS: int = 1000

    # HTTP Client Config
    # Request timeout (seconds)
    HTTP_TIMEOUT: int = 1800
    # Whether provider base URLs may use private/internal IP addresses
    ALLOW_PRIVATE_IP_PROVIDER: bool = False

    # API Key Config
    # Generated API Key prefix
    API_KEY_PREFIX: str = "lgw-"
    # API Key length (excluding prefix)
    API_KEY_LENGTH: int = 32

    # Admin Login Authentication
    # Enables login authentication when both ADMIN_USERNAME and ADMIN_PASSWORD are set; otherwise, login is not required.
    ADMIN_USERNAME: str | None = None
    ADMIN_PASSWORD: str | None = None
    # Admin login token TTL (seconds)
    ADMIN_TOKEN_TTL_SECONDS: int = 86400

    # KV Store Config
    # KV store backend: "database" uses the SQL database, "redis" uses Redis
    KV_STORE_TYPE: Literal["database", "redis"] = "database"
    # Redis connection URL (only used when KV_STORE_TYPE is "redis")
    REDIS_URL: str = "redis://localhost:6379/0"

    # Log Cleanup Config
    # Log retention days (default 90 days)
    LOG_RETENTION_DAYS: int = 90
    # Log cleanup interval in hours (default 24 hours)
    LOG_CLEANUP_INTERVAL_HOURS: int = 24
    # Soft threshold ratio for quota-aware routing
    QUOTA_AWARE_SOFT_LIMIT_RATIO: float = 0.8
    # Cooldown after quota-related provider failures
    QUOTA_AWARE_COOLDOWN_SECONDS: int = 900

    # CORS Config
    # Comma-separated list of allowed origins for CORS
    # Example: "http://localhost:3000,https://example.com"
    # Default: empty list (no CORS allowed in production)
    ALLOWED_ORIGINS: str = ""

    # Encryption Config
    # Encryption key for sensitive data (e.g., API keys)
    # Generate with: python -c "import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
    # WARNING: Changing this key will make previously encrypted data unreadable
    ENCRYPTION_KEY: str | None = None
    # Whether API keys can be viewed/copied again in admin API Key list
    ENABLE_VIEW_API_KEYS: bool = False

    # Rate Limit Config
    # Enable/disable rate limiting (useful for development)
    RATE_LIMIT_ENABLED: bool = False
    # Default rate limit for general endpoints
    RATE_LIMIT_DEFAULT: str = "100/minute"
    # Rate limit for admin API endpoints
    RATE_LIMIT_ADMIN: str = "20/minute"
    # Rate limit for proxy endpoints (/v1/*)
    RATE_LIMIT_PROXY: str = "200/minute"

    model_config = SettingsConfigDict(
        env_file=ENV_FILES,
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


@lru_cache()
def get_settings() -> Settings:
    """
    Get application configuration (Singleton)

    Uses lru_cache to ensure configuration is loaded only once, improving performance.

    Returns:
        Settings: Application configuration instance
    """
    return Settings()
