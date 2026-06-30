"""Application configuration, loaded from environment variables (and `.env`).

We use `pydantic-settings` so configuration is *typed* and *validated at startup*.
If a required variable like OPENROUTER_API_KEY is missing, the program stops
immediately with a clear error — instead of breaking mysteriously later on.

Two complementary tools, on purpose:
  - `load_dotenv()` reads `.env` into the real environment (`os.environ`). This is
    what third-party libraries that read env vars directly need — e.g. `fal_client`
    looks for `FAL_KEY` in the environment on its own.
  - `Settings` (below) gives *our* code typed, validated, autocompleted access.

Usage anywhere in the code:

    from agent.config import get_settings
    settings = get_settings()
    print(settings.llm_model)
"""

from functools import lru_cache
from typing import Literal

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Populate os.environ from .env as early as possible, for libraries that read it.
load_dotenv()


class Settings(BaseSettings):
    """All configuration for this project, in one validated place."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM (required) -----------------------------------------------------
    # Get a key at https://openrouter.ai/keys
    openrouter_api_key: str = Field(description="Your OpenRouter API key.")
    # Which model each tier maps to lives in code — see agent/services/llm.py.

    # --- Environment --------------------------------------------------------
    # Which environment this process runs in. The SAME code reads the SAME setting
    # names; only the *values* differ between your laptop (.env) and the deployed
    # app (Railway variables). Used e.g. to choose polling vs. webhook for a bot.
    # See examples/inspiration_bot for a worked example of environments.
    environment: Literal["development", "production"] = Field(
        default="development", description="development (local) or production (deployed)."
    )

    # --- Logging ------------------------------------------------------------
    # How chatty the logs are: DEBUG, INFO, WARNING, ERROR.
    log_level: str = Field(default="INFO", description="Logging verbosity.")

    # --- Web app gate (optional) --------------------------------------------
    # If set, web demos require this password before showing anything. Leave unset
    # for local development; set it for anything you deploy publicly.
    app_password: str | None = Field(default=None, description="Password for deployed web apps.")

    # --- Admin panel (optional) ---------------------------------------------
    # Set ADMIN_PASSWORD to enable the /admin interface. Without it the panel
    # is disabled (returns 404) even in production.
    admin_password: str | None = Field(default=None, description="Password for the /admin panel.")
    admin_username: str = Field(default="admin", description="Username for the full admin login.")

    # --- Media: fal.ai (optional) -------------------------------------------
    # Get a key at https://fal.ai/dashboard/keys. Needed for the media service.
    fal_key: str | None = Field(default=None, description="fal.ai API key (FAL_KEY).")

    # --- Storage: Cloudflare R2 / S3 (optional) -----------------------------
    # From your R2 dashboard. Needed for the storage service.
    r2_account_id: str | None = Field(default=None, description="Cloudflare account id.")
    r2_access_key_id: str | None = Field(default=None, description="R2 access key id.")
    r2_secret_access_key: str | None = Field(default=None, description="R2 secret access key.")
    r2_bucket: str | None = Field(default=None, description="R2 bucket name.")
    # If the bucket is shared, this prefix scopes everything you store into your
    # own slice of it (e.g. "student07"). The storage service adds/strips it for you.
    r2_prefix: str = Field(
        default="", description="Key prefix that scopes your slice of the bucket."
    )
    # Public/custom domain for the bucket (e.g. https://files.example.com). When
    # set, storage.public_url() builds stable, non-expiring links for your files.
    r2_public_base_url: str | None = Field(
        default=None, description="Public base URL (custom domain) for the bucket."
    )

    # --- Database: Neon Postgres (optional) ---------------------------------
    # Neon gives you an async-ready Postgres URL (postgresql://...).
    database_url: str | None = Field(
        default=None,
        description="Neon Postgres connection string. Needed for the db service.",
    )

    # --- Telegram bot (optional; used by examples/inspiration_bot) ----------
    # IMPORTANT: use a DIFFERENT token per environment. Telegram allows only one
    # consumer per token, so a dev poller and a prod webhook on the same token
    # collide (409 Conflict). Make a dev bot and a prod bot with @BotFather.
    telegram_bot_token: str | None = Field(
        default=None, description="Bot token from @BotFather. One per environment."
    )
    # The secret_token we register with the webhook; Telegram echoes it back in the
    # X-Telegram-Bot-Api-Secret-Token header so we can reject forged calls (prod).
    telegram_webhook_secret: str | None = Field(
        default=None, description="Shared secret verifying webhook calls came from Telegram."
    )
    # Public base URL of the deployed app (e.g. https://my-bot.up.railway.app), used
    # to register the webhook. Unset locally → the bot uses long polling instead.
    public_url: str | None = Field(
        default=None, description="Public base URL of the deployed app (for the webhook)."
    )
    # Secret required on the POST /cron/tick endpoint so only the scheduler can call it.
    cron_secret: str | None = Field(
        default=None, description="Secret required to call the cron endpoint."
    )
    # Comma-separated Telegram user ids allowed to use the bot (authorization, on top
    # of Telegram's authentication). Empty = open to anyone (fine for local dev).
    allowed_telegram_ids: str = Field(
        default="", description="Comma-separated allowed Telegram user ids. Empty = open."
    )

    @property
    def allowed_ids(self) -> set[int]:
        """Parse `allowed_telegram_ids` into a set of ints (commas or spaces)."""
        return {int(p) for p in self.allowed_telegram_ids.replace(",", " ").split() if p.strip()}

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    """Return the settings, loaded once and cached.

    Always call this instead of constructing `Settings()` yourself, so the
    `.env` file is read a single time.
    """
    # pydantic-settings fills required fields from the environment at runtime,
    # which the type checker can't see — hence the ignore on this one line.
    return Settings()  # pyright: ignore[reportCallIssue]
