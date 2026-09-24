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
    # "demo" or "live" - both make real Deriv API calls, just against
    # different accounts (see deriv_credential_names below). Default to demo
    # so a fresh deploy can never touch real money by accident.
    execution_mode: str = "demo"
    emergency_stop: bool = False

    deriv_app_id: str = ""
    deriv_api_token: str = ""
    deriv_account_id: str = "ROT90786324"
    deriv_demo_api_token: str = ""
    deriv_demo_account_id: str = ""
    deriv_ws_url: str = "wss://ws.derivws.com/websockets/v3"
    # Deriv enforces a fixed, symbol-specific minimum multiplier for Multiplier
    # contracts (e.g. 40x-2000x on most synthetic indices) with no way to trade
    # lower. Entries on a symbol whose minimum exceeds this get refused, so a
    # stray/misconfigured alert can't burn capital on a near-guaranteed
    # stop-out. Set high (above the highest known floor, 7500x on Step 100) at
    # the user's request to allow every symbol through on the demo account -
    # lower this again before trading real money on symbols above ~20x-50x.
    max_multiplier_floor: int = 10000

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
# The dashboard toggle overrides settings.execution_mode in-process (see
# get_risk_settings) so trading code doesn't need a db session to check it.
# Keep the original .env value around so the toggle can fall back to it.
ENV_EXECUTION_MODE = settings.execution_mode


def deriv_credential_names(mode: str) -> dict[str, str]:
    """Env var names -> current values for whichever Deriv account `mode` targets.

    Anything other than exactly "live" (including a stale "paper" left over
    from before demo/live existed) resolves to the demo account - real-money
    execution requires an explicit, exact "live", never a fallback.
    """
    if mode.lower() == "live":
        return {
            "DERIV_APP_ID": settings.deriv_app_id,
            "DERIV_API_TOKEN": settings.deriv_api_token,
            "DERIV_ACCOUNT_ID": settings.deriv_account_id,
        }
    return {
        "DERIV_APP_ID": settings.deriv_app_id,
        "DERIV_DEMO_API_TOKEN": settings.deriv_demo_api_token,
        "DERIV_DEMO_ACCOUNT_ID": settings.deriv_demo_account_id,
    }
