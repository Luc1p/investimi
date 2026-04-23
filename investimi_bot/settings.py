from __future__ import annotations

from dataclasses import dataclass

from dotenv import load_dotenv
import os


def _parse_csv_ints(v: str | None) -> set[int] | None:
    if not v:
        return None
    items = [x.strip() for x in v.split(",") if x.strip()]
    out: set[int] = set()
    for item in items:
        out.add(int(item))
    return out


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_allowed_chat_ids: set[int] | None
    telegram_channel_id: int | None
    fred_api_key: str | None
    fmp_api_key: str | None
    twelve_data_api_key: str | None
    sec_user_agent: str | None
    congress_trades_user_agent: str | None
    congress_senate_url: str | None
    congress_house_url: str | None


def load_settings() -> Settings:
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN in environment/.env")
    allowed = _parse_csv_ints(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS"))
    channel_id = os.getenv("TELEGRAM_CHANNEL_ID")
    fred_key = os.getenv("FRED_API_KEY")
    fmp_key = os.getenv("FMP_API_KEY")
    td_key = os.getenv("TWELVE_DATA_API_KEY")
    sec_ua = os.getenv("SEC_USER_AGENT")
    congress_ua = os.getenv("CONGRESS_TRADES_USER_AGENT")
    congress_senate_url = os.getenv("CONGRESS_SENATE_URL")
    congress_house_url = os.getenv("CONGRESS_HOUSE_URL")
    return Settings(
        telegram_bot_token=token,
        telegram_allowed_chat_ids=allowed,
        telegram_channel_id=int(channel_id) if channel_id and channel_id.strip() else None,
        fred_api_key=fred_key.strip() if fred_key else None,
        fmp_api_key=fmp_key.strip() if fmp_key else None,
        twelve_data_api_key=td_key.strip() if td_key else None,
        sec_user_agent=sec_ua.strip() if sec_ua else None,
        congress_trades_user_agent=congress_ua.strip() if congress_ua else None,
        congress_senate_url=congress_senate_url.strip() if congress_senate_url else None,
        congress_house_url=congress_house_url.strip() if congress_house_url else None,
    )

