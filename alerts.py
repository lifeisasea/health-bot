"""Уведомления владельцу в Telegram о сбоях (бэкап/Garmin).

Защита от спама: одно и то же уведомление (по ключу) шлётся не чаще раза в N часов.
Состояние дедупликации хранится в БД (profile) — переживает перезапуски.
Шлём напрямую через Bot API (sendMessage), чтобы работало и из фоновых потоков.
"""
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import config

log = logging.getLogger("health-bot.alerts")


def notify(key: str, text: str, cooldown_hours: float = 6) -> None:
    import db  # ленивый импорт, чтобы избежать циклов

    now = datetime.now(timezone.utc)
    last = db.get_profile(f"alert_{key}", "")
    if last:
        try:
            if now - datetime.fromisoformat(last) < timedelta(hours=cooldown_hours):
                return  # уже уведомляли недавно — не спамим
        except ValueError:
            pass
    if not (config.TELEGRAM_BOT_TOKEN and config.OWNER_ID):
        return
    try:
        data = urllib.parse.urlencode({"chat_id": config.OWNER_ID, "text": text}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            data=data,
            timeout=15,
        )
        db.set_profile(f"alert_{key}", now.isoformat())
    except Exception as e:
        log.warning("Не удалось отправить уведомление: %s", e)
