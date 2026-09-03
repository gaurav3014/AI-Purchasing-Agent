from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    google_api_key: str = ""
    google_model: str = "gemini-3.8-flash"
    use_stub_llm: bool = False
    database_url: str = "sqlite:///./purchasing_agent.db"
    port: int = 8000

    # The cron (see app/scheduler.py) scans all product data and reviews
    # everything pending across all three scenarios once a day by default,
    # in addition to the manual "Review now" button in the UI -- both paths
    # call the exact same review logic. Override for faster local testing,
    # e.g. SCENARIO1_CRON_INTERVAL_SECONDS=60. Set to 0 to disable the cron.
    scenario1_cron_interval_seconds: int = 24 * 60 * 60

    model_config = SettingsConfigDict(env_file="../.env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
