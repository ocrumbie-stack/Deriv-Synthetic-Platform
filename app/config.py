from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Trading Execution Platform"
    database_url: str = "sqlite:///./trading_platform.db"
    webhook_secret: str = "change-me"
    execution_mode: str = "paper"
    emergency_stop: bool = False

    bitget_api_key: str = ""
    bitget_api_secret: str = ""
    bitget_api_passphrase: str = ""
    bitget_base_url: str = "https://api.bitget.com"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
