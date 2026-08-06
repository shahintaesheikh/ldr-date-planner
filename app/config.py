from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # --- Database ---
    database_url: str = "postgresql+asyncpg://localhost:5432/ldr_date"

    # --- FastAPI ---
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_version: str = "0.1.0"

    # --- LangSmith ---
    langsmith_api_key: str | None = None
    langsmith_project: str = "ldr-date-planner"

    # --- Model Provider ---
    anthropic_api_key: str | None = None

    # --- Twilio ---
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_phone_number: str | None = None

    # --- Calendar Providers ---
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_redirect_uri: str | None = None

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}