from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/hotels"
    rabbit_url: str = "amqp://guest:guest@localhost:5672/"
    # No default on purpose: the app must not start with a guessable key.
    jwt_secret: str = Field(min_length=32)
    jwt_ttl_minutes: int = 60

    demo_supplier_url: str = "http://localhost:8001"
    # Public page about this bot, e.g. the repository. Wikimedia requires a contact in
    # the User-Agent, so the wikivoyage source stays off until it is set.
    contact_url: str = ""
    http_timeout: float = 40.0
    http_attempts: int = 3
    http_backoff: float = 1.0
    # Minimum pause between two requests to the same host, seconds.
    host_interval: float = 5.0
    demo_interval: float = 0.05

    # Delays before the 2nd, 3rd and 4th attempt of a job, seconds.
    retry_delays: list[int] = [30, 120, 600]
    # Pause before giving a message back when the database is down, seconds.
    requeue_pause: float = 5.0
    queue_prefix: str = "parse"

    scheduler_interval_hours: float = 12
    stale_job_minutes: int = 15

    @property
    def user_agent(self) -> str:
        return f"hotel-aggregator/0.1 ({self.contact_url or 'educational project'})"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
