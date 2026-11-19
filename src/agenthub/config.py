from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTHUB_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://agenthub:agenthub@localhost:5432/agenthub"
    # Fernet key (urlsafe base64, 32 bytes) used to encrypt provider credentials at rest.
    credential_key: str = Field(default="", repr=False)
    # Base URL used to build the OAuth redirect.
    public_base_url: str = "http://localhost:8000"

    discord_bot_token: str = Field(default="", repr=False)
    discord_client_id: str = ""
    discord_client_secret: str = Field(default="", repr=False)
    discord_guild_id: str = ""  # if set, slash commands sync to this guild only (fast)
    discord_projection_channel_id: str = ""  # channel where events are mirrored

    github_webhook_secret: str = Field(default="", repr=False)

    # Limits
    max_agents_per_user: int = 10
    provider_timeout_seconds: float = 60.0
    max_event_hops: int = 6
    default_agent_budget_tokens: int = 200_000
    default_agent_budget_usd: float = 5.0
    oauth_state_ttl_seconds: int = 600


@lru_cache
def get_settings() -> Settings:
    return Settings()
