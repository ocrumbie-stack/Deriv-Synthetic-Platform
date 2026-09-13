import os

from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_database_url() -> str:
    # Railway's filesystem is ephemeral and gets wiped on every deploy unless
    # a volume is mounted. Once one is mounted, RAILWAY_VOLUME_MOUNT_PATH is
    # set automatically, so put the SQLite file there instead of losing it.
    volume_path = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if volume_path:
        return f"sqlite:///{volume_path}/trading_platform.db"
    return "sqlite:///./trading_platform.db"


class Settings(BaseSettings):
    app_name: str = "Deriv Synthetic Trading Platform"
    database_url: str = _default_database_url()
    webhook_secret: str = "change-me"
    execution_mode: str = "paper"
    emergency_stop: bool = False

    deriv_app_id: str = ""
    deriv_api_token: str = ""
    deriv_account_id: str = "ROT90786324"
    deriv_ws_url: str = "wss://ws.derivws.com/websockets/v3"
    deriv_multiplier: int = 100

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
