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


def sync_tokens() -> None:
    """Скачать свежие токены из датасета (их каждые 8 ч обновляет домашний Raspberry Pi)
    и сбросить сессию. Так сервер всегда использует уже готовый валидный токен и НЕ
    обращается к oauth-сервису Garmin (который для IP Render заблокирован → 429)."""
    global _client_cache
    TOKDIR.mkdir(parents=True, exist_ok=True)
    got = False
    for f in _TOKEN_FILES:
        if persistence.download(f"garmin/{f}", TOKDIR / f):
            got = True
    if got:
        _client_cache = None  # пересоздать сессию из свежих токенов


_client_cache = None
_COOLDOWN_KEY = "garmin_cooldown_until"  # ISO-время в profile (переживает перезапуски)


def _cooldown_active() -> bool:
    import db

    until = db.get_profile(_COOLDOWN_KEY, "")
    if not until:
        return False
    try:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc) < datetime.fromisoformat(until)
    except ValueError:
        return False


def client():
    """Одна сессия на весь процесс — НЕ логинимся каждый раз (иначе Garmin даёт 429).
    garth внутри сам обновляет токен по мере необходимости.
    После 429 уходим в «тишину» на час; пауза хранится в БД, поэтому перезапуск
    сервера её не сбрасывает (иначе можно случайно растревожить лимит Garmin)."""
    global _client_cache
    import db

    if _client_cache is not None:
        return _client_cache
    if _cooldown_active():
        raise RuntimeError("Garmin на паузе после 429 — ждём сброса лимита")

    had_cooldown = bool(db.get_profile(_COOLDOWN_KEY, ""))
    try:
        import garminconnect

        g = garminconnect.Garmin()
        g.login(str(TOKDIR))
        _client_cache = g
        if had_cooldown:  # были в паузе и снова получилось — снять и сообщить
            db.set_profile(_COOLDOWN_KEY, "")
            try:
                import alerts

                alerts.notify("garmin_ok", "✅ Garmin снова на связи — данные с часов обновляются.")
            except Exception:
                pass
        return g
    except Exception as e:
        if "429" in str(e) or "Too Many Requests" in str(e):
            from datetime import datetime, timedelta, timezone

            db.set_profile(
                _COOLDOWN_KEY,
                (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            )
            log.warning("Garmin 429 — пауза на час (сохранена в БД)")
            try:
                import alerts

                alerts.notify(
                    "garmin_429",
                    "⚠️ Garmin временно ограничил доступ (лимит запросов). Данные с часов "
                    "пока не обновляются — обычно проходит само за час-два. Если надолго — "
                    "напиши, освежим вход.",
                )
            except Exception:
                pass
        raise


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
    # заряд после сна — главный показатель восстановления (вечером BB всегда низкий)
    out["body_battery_wake"] = _num(
        summ.get("bodyBatteryAtWakeTime"), summ.get("bodyBatteryHighestValue")
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
