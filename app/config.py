from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Deriv Synthetic Trading Platform"
    database_url: str = "sqlite:///./trading_platform.db"
    webhook_secret: str = "change-me"
    execution_mode: str = "paper"
    emergency_stop: bool = False

    deriv_app_id: str = ""
    deriv_api_token: str = ""
    deriv_ws_url: str = "wss://ws.derivws.com/websockets/v3"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
