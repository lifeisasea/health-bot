"""Загрузка настроек из .env."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
PHOTOS_DIR = DATA_DIR / "photos"
PHOTOS_DIR.mkdir(exist_ok=True)
GARMIN_TOKENS_DIR = DATA_DIR / "garmin_tokens"

load_dotenv(BASE_DIR / ".env")

MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY", "").strip()
MINIMAX_BASE_URL = os.getenv("MINIMAX_BASE_URL", "https://api.minimax.io/v1").strip()
MINIMAX_MODEL = os.getenv("MINIMAX_MODEL", "MiniMax-M3").strip()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")

# Бэкап базы в приватный HF Dataset (чтобы история не терялась на бесплатном хостинге).
HF_TOKEN = os.getenv("HF_TOKEN", "").strip()
HF_BACKUP_REPO = os.getenv("HF_BACKUP_REPO", "").strip()  # вид: <username>/health-bot-data

TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow").strip()
DAILY_SUMMARY_TIME = os.getenv("DAILY_SUMMARY_TIME", "21:30").strip()
WEEKLY_SUMMARY_DAY = os.getenv("WEEKLY_SUMMARY_DAY", "sun").strip()
WEEKLY_SUMMARY_TIME = os.getenv("WEEKLY_SUMMARY_TIME", "10:00").strip()

DB_PATH = DATA_DIR / "health.db"


def check() -> list[str]:
    """Вернуть список незаполненных полей, без которых бот не запустится.

    OWNER_ID сюда НЕ входит: без него бот стартует и отвечает всем, чтобы можно
    было узнать свой ID командой /id. После заполнения OWNER_ID бот отвечает только владельцу.
    """
    missing = []
    if not MINIMAX_API_KEY:
        missing.append("MINIMAX_API_KEY")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    return missing
