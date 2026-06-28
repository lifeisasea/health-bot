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


_client_cache = None
_cooldown_until = 0.0  # monotonic-время, до которого не дёргаем Garmin (после 429)


def client():
    """Одна сессия на весь процесс — НЕ логинимся каждый раз (иначе Garmin даёт 429).
    garth внутри сам обновляет токен по мере необходимости (~раз в час).
    После 429 уходим в «тишину» на час, чтобы лимит Garmin успел сброситься."""
    global _client_cache, _cooldown_until
    import time

    if _client_cache is not None:
        return _client_cache
    if time.monotonic() < _cooldown_until:
        raise RuntimeError("Garmin на паузе после 429 — ждём сброса лимита")
    try:
        import garminconnect

        g = garminconnect.Garmin()
        g.login(str(TOKDIR))
        _client_cache = g
        return g
    except Exception as e:
        if "429" in str(e) or "Too Many Requests" in str(e):
            _cooldown_until = time.monotonic() + 3600  # час тишины
            log.warning("Garmin 429 — пауза на час, чтобы лимит сбросился")
        raise


def reset_client():
    global _client_cache
    _client_cache = None


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


# человекочитаемые названия типов тренировок
_TYPE_RU = {
    "lap_swimming": "Плавание", "open_water_swimming": "Плавание (открытая вода)",
    "swimming": "Плавание", "running": "Бег", "treadmill_running": "Бег (дорожка)",
    "walking": "Ходьба", "hiking": "Хайкинг", "cycling": "Велосипед",
    "indoor_cycling": "Велотренажёр", "strength_training": "Силовая",
    "yoga": "Йога", "pilates": "Пилатес", "cardio": "Кардио",
    "elliptical": "Эллипс", "fitness_equipment": "Тренажёры",
}


def type_ru(typekey: Optional[str]) -> str:
    if not typekey:
        return "Тренировка"
    return _TYPE_RU.get(typekey, typekey.replace("_", " ").capitalize())


def fetch_activities(c, start_date: str, end_date: str) -> list:
    """Тренировки (заплывы, бег, силовая и т.п.) за период."""
    try:
        acts = c.get_activities_by_date(start_date, end_date)
    except Exception as e:
        log.warning("garmin activities: %s", e)
        return []
    out = []
    for a in acts or []:
        try:
            dur = a.get("duration") or 0
            dist = a.get("distance") or 0
            out.append({
                "activity_id": a.get("activityId"),
                "date": (a.get("startTimeLocal") or "")[:10],
                "type": (a.get("activityType") or {}).get("typeKey"),
                "name": a.get("activityName"),
                "duration_min": round(dur / 60, 1) if dur else None,
                "distance_km": round(dist / 1000, 2) if dist else None,
                "calories": a.get("calories"),
            })
        except Exception:
            continue
    return out
