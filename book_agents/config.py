from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"

    redis_url: str = "redis://localhost:6379/0"
    stream_key: str = "book_agents:events"
    dlq_stream_key: str = "book_agents:events:dlq"

    database_url: str = "postgresql+asyncpg://book:book@localhost:5432/book_agents"

    log_level: str = "INFO"
    max_rewrites_per_chapter: int = 3

    ui_poll_seconds: float = 1.0
    trace_max_steps: int = 500

settings = Settings()
