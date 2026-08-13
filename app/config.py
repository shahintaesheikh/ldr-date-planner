from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # --- App ---
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_version: str = "0.1.0"
    app_base_url: str = "http://localhost:8000"

    # --- Database ---
    database_url: str = "postgresql+asyncpg://localhost:5432/ldr_date"

    # --- LangSmith ---
    langsmith_api_key: str | None = None
    langsmith_project: str = "ldr-date-planner"

    # --- Model Provider ---
    anthropic_api_key: str | None = None

    # --- Embeddings ---
    openai_api_key: str | None = None
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    embedding_model_version: str = "openai-text-embedding-3-small-2025-01"

    # --- Twilio ---
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_phone_number: str | None = None
    twilio_status_callback_url: str | None = None

    # --- Calendar Providers ---
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_redirect_uri: str | None = None

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}