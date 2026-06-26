"""Garmin Connect.

Вход — по токенам, которые пользователь минтит локально (garmin_login.py) и
кладёт в приватный датасет. Бот скачивает токены и логинится ими; пароль на
сервере не хранится. Токены garth обновляются автоматически.
"""
import logging
from typing import Optional

import config
import persistence

log = logging.getLogger("health-bot.garmin")

TOKDIR = config.GARMIN_TOKENS_DIR
_TOKEN_FILES = ["oauth1_token.json", "oauth2_token.json"]


def tokens_present() -> bool:
    return TOKDIR.exists() and all((TOKDIR / f).exists() for f in _TOKEN_FILES)


def restore_tokens() -> bool:
    """Скачать токены Garmin из датасета (туда их кладёт garmin_login.py)."""
    if tokens_present():
        return True
    TOKDIR.mkdir(parents=True, exist_ok=True)
    for f in _TOKEN_FILES:
        persistence.download(f"garmin/{f}", TOKDIR / f)
    if tokens_present():
        log.info("Токены Garmin восстановлены из датасета.")
        return True
    log.info("Токенов Garmin нет — раздел Garmin отключён (запусти garmin_login.py).")
    return False


def available() -> bool:
    return tokens_present()


def client():
    import garminconnect

    g = garminconnect.Garmin()
    g.login(str(TOKDIR))
    return g


def _num(*vals):
    for v in vals:
        if isinstance(v, (int, float)):
            return v
    return None


def fetch_day(c, datestr: str) -> dict:
    """Собрать ключевые метрики за день. Любой источник может отсутствовать."""
    def safe(fn):
        try:
            return fn()
        except Exception as e:
            log.debug("garmin %s %s: %s", datestr, getattr(fn, "__name__", "?"), e)
            return None

    summ = safe(lambda: c.get_user_summary(datestr)) or {}
    sleep = safe(lambda: c.get_sleep_data(datestr)) or {}
    tr = safe(lambda: c.get_training_readiness(datestr)) or []
    vo2 = safe(lambda: c.get_max_metrics(datestr)) or []
    hrv = safe(lambda: c.get_hrv_data(datestr)) or {}

    out = {"date": datestr}
    out["steps"] = _num(summ.get("totalSteps"))
    out["resting_hr"] = _num(summ.get("restingHeartRate"))
    out["stress_avg"] = _num(summ.get("averageStressLevel"))
    out["body_battery"] = _num(
        summ.get("bodyBatteryMostRecentValue"), summ.get("bodyBatteryHighestValue")
    )

    dto = (sleep.get("dailySleepDTO") or {}) if isinstance(sleep, dict) else {}
    secs = _num(dto.get("sleepTimeSeconds"))
    out["sleep_hours"] = round(secs / 3600, 1) if secs else None
    scores = dto.get("sleepScores") or {}
    out["sleep_score"] = _num((scores.get("overall") or {}).get("value"))

    if isinstance(tr, list) and tr:
        out["training_readiness"] = _num((tr[0] or {}).get("score"))
    if isinstance(vo2, list) and vo2:
        gen = (vo2[0] or {}).get("generic") or {}
        out["vo2max"] = _num(gen.get("vo2MaxPreciseValue"), gen.get("vo2MaxValue"))
    if isinstance(hrv, dict):
        out["hrv"] = _num((hrv.get("hrvSummary") or {}).get("lastNightAvg"))
    return out
