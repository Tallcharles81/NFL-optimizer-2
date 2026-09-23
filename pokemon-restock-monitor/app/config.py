"""Application configuration.

All settings come from environment variables (optionally loaded from a ``.env``
file). Secrets (webhook URLs, bot tokens, SMTP passwords) must only ever be
provided this way -- never hard-coded.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Hard floor for polling real retailers. Per-product / per-retailer overrides
# can never go below this. The simulated retailer is exempt.
ABSOLUTE_MIN_POLL_SECONDS = 15


@dataclass(frozen=True)
class StoreConfig:
    retailer: str
    store_id: str
    name: str
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None


def _parse_store_entry(retailer: str, raw: str) -> StoreConfig:
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 2 or not parts[0]:
        raise ValueError(
            f"Invalid store entry {raw!r}: expected 'store_id|name|city|state|zip'"
        )
    parts += [""] * (5 - len(parts))
    store_id, name, city, state, zip_code = parts[:5]
    return StoreConfig(
        retailer=retailer.lower(),
        store_id=store_id,
        name=name or store_id,
        city=city or None,
        state=state or None,
        zip_code=zip_code or None,
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- Application -------------------------------------------------------
    app_name: str = "Pokémon Restock Monitor"
    environment: str = "production"
    database_url: str = "sqlite:///./data/restock.db"
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    log_format: str = Field("console", description="console | json")
    timezone: str = "America/New_York"
    dashboard_username: str = "admin"
    dashboard_password: str | None = None  # when set, the dashboard/API require basic auth

    # --- Monitoring ----------------------------------------------------------
    monitor_enabled: bool = True
    scheduler_tick_seconds: float = 5
    max_concurrent_checks: int = 4
    poll_interval_seconds: float = 90
    poll_jitter_seconds: float = 30
    recently_active_interval_seconds: float = 45
    recently_active_jitter_seconds: float = 15
    recently_active_window_minutes: float = 30
    available_hold_interval_seconds: float = 300
    error_backoff_base_seconds: float = 60
    error_backoff_max_seconds: float = 3600
    stall_alert_minutes: float = 30

    # Retailers that only tolerate a gentle pace: at most N product checks per sweep,
    # least recently checked first. Format: "walmart=1;other=2".
    checks_per_sweep: str = "walmart=1"

    # --- Rate limiting / HTTP -------------------------------------------------
    min_request_interval_seconds: float = 5
    max_retries: int = 3
    retry_base_delay_seconds: float = 2
    retry_max_delay_seconds: float = 30
    request_timeout_seconds: float = 15
    circuit_breaker_threshold: int = 5
    circuit_breaker_cooldown_seconds: float = 1800
    blocked_cooldown_seconds: float = 21600
    robots_cache_seconds: float = 86400
    contact_email: str = "unset@example.com"
    user_agent: str = "PokemonRestockMonitor/1.0 (personal stock alert bot; contact: {contact})"

    # --- Verification / alert policy -----------------------------------------
    verification_delay_seconds: float = 10
    verification_checks: int = 1
    accept_third_party: bool = False
    accept_unknown_seller: bool = True
    alert_on_limited: bool = True
    alert_on_preorder: bool = False
    alert_on_unknown_to_available: bool = True
    pending_notification_max_age_minutes: float = 15
    # Repeat the alert while the item stays in stock (0 = no reminders).
    restock_reminder_minutes: float = 10
    restock_reminder_max: int = 6
    # Add a "BUY WITH GOOGLE" button to Target alerts. It opens Google AI Mode asking to buy
    # the item, where Google's approved shopping agent can check out after you confirm.
    buy_with_google_button: bool = True

    # --- Notifications -------------------------------------------------------
    notify_console: bool = True
    discord_webhook_url: str | None = None
    discord_mention: str | None = None  # e.g. "@here" or "<@&ROLE_ID>"
    discord_use_buttons: bool = True
    ntfy_topic: str | None = None  # phone push via the ntfy app
    ntfy_server: str = "https://ntfy.sh"
    ntfy_token: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    email_enabled: bool = False
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool = True
    email_from: str | None = None
    email_to: str | None = None

    # --- Stores ----------------------------------------------------------------
    # Format: "store_id|name|city|state|zip;store_id|name|city|state|zip"
    target_stores: str = ""
    # Format: "retailer|store_id|name|city|state|zip;..."
    extra_stores: str = ""

    # --- Retailer specific -----------------------------------------------------
    target_use_browser: bool = False

    # --- Analytics -------------------------------------------------------------
    analytics_min_episodes: int = 5

    @field_validator("log_format")
    @classmethod
    def _check_log_format(cls, v: str) -> str:
        v = v.lower()
        if v not in {"console", "json"}:
            raise ValueError("LOG_FORMAT must be 'console' or 'json'")
        return v

    def checks_per_sweep_limits(self) -> dict[str, int]:
        limits = {}
        for entry in filter(None, (e.strip() for e in self.checks_per_sweep.split(";"))):
            slug, _, n = entry.partition("=")
            if not n.strip().isdigit() or int(n) < 1:
                raise ValueError(f"Invalid CHECKS_PER_SWEEP entry {entry!r}: expected retailer=N")
            limits[slug.strip().lower()] = int(n)
        return limits

    @property
    def effective_user_agent(self) -> str:
        return self.user_agent.format(contact=self.contact_email)

    def configured_stores(self) -> list[StoreConfig]:
        stores: list[StoreConfig] = []
        for entry in filter(None, (e.strip() for e in self.target_stores.split(";"))):
            stores.append(_parse_store_entry("target", entry))
        for entry in filter(None, (e.strip() for e in self.extra_stores.split(";"))):
            retailer, _, rest = entry.partition("|")
            if not rest:
                raise ValueError(f"Invalid EXTRA_STORES entry {entry!r}")
            stores.append(_parse_store_entry(retailer.strip(), rest))
        return stores

    def secret_fields(self) -> set[str]:
        return {
            "discord_webhook_url",
            "ntfy_topic",
            "ntfy_token",
            "telegram_bot_token",
            "smtp_password",
            "dashboard_password",
            "database_url",
        }

    def public_dict(self) -> dict:
        """Settings safe to display on the dashboard (secrets masked)."""
        out = {}
        for key, value in self.model_dump().items():
            if key in self.secret_fields():
                out[key] = "•••• configured" if value else "not set"
            else:
                out[key] = value
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()
