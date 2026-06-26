"""Telegram-бот: дневник питания + советы по запросу (Этап 1).

Запуск:  python bot.py
"""
import asyncio
import logging
import os
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import db
import persistence
import prompts
from llm import chat, chat_json

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("health-bot")

bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# Бот отвечает только владельцу.
if config.OWNER_ID:
    dp.message.filter(F.from_user.id == config.OWNER_ID)


# ---------------- команды ----------------

@dp.message(Command("start", "help"))
async def cmd_help(m: Message):
    await m.answer(
        "Привет! Я слежу за твоим питанием и помогаю советами.\n\n"
        "📷 Пришли фото еды — оценю КБЖУ и запишу.\n"
        "💬 Спроси что угодно — «что выбрать на обед?», «полезно ли это съесть?», "
        "«какая активность сегодня?» — отвечу с учётом твоих данных.\n\n"
        "Команды:\n"
        "/today — что съедено за день\n"
        "/summary — разбор питания за сегодня сейчас\n"
        "/allergies <текст> — указать аллергии\n"
        "/goal <текст> — изменить цель\n"
        "/sick <причина> — отметить, что болеешь\n"
        "/recovered — отметить выздоровление\n"
        "/ivf_on <заметка> — начать программу ЭКО\n"
        "/ivf_off — завершить программу ЭКО\n"
        "/injury <заметка> — отметить травму\n"
        "/state — мои активные состояния\n\n"
        "⚠️ Я помощник, а не врач: по медицинским вопросам обращайся к специалисту."
    )


@dp.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Твой Telegram ID: {m.from_user.id}\nВпиши его в .env как OWNER_ID и перезапусти бота.")


@dp.message(Command("allergies"))
async def cmd_allergies(m: Message, command: CommandObject):
    if command.args:
        db.set_profile("allergies", command.args.strip())
        await m.answer(f"Записала аллергии: {command.args.strip()}")
    else:
        await m.answer(f"Текущие аллергии: {db.get_profile('allergies', 'не указаны')}")


@dp.message(Command("goal"))
async def cmd_goal(m: Message, command: CommandObject):
    if command.args:
        db.set_profile("goal", command.args.strip())
        await m.answer(f"Цель обновлена: {command.args.strip()}")
    else:
        await m.answer(f"Текущая цель: {db.get_profile('goal', prompts.GOAL_DEFAULT)}")


@dp.message(Command("sick"))
async def cmd_sick(m: Message, command: CommandObject):
    reason = (command.args or "недомогание").strip()
    db.add_state("illness", reason)
    await m.answer(f"Отметила, что ты болеешь: {reason}. Учту это в сводках. Выздоровеешь — напиши /recovered.")


@dp.message(Command("recovered"))
async def cmd_recovered(m: Message):
    n = db.end_states("illness")
    await m.answer("Отметила выздоровление 🌿" if n else "Активной болезни не было записано.")


@dp.message(Command("ivf_on"))
async def cmd_ivf_on(m: Message, command: CommandObject):
    note = (command.args or "программа ЭКО").strip()
    db.add_state("ivf", note)
    await m.answer(f"Отметила программу ЭКО: {note}. Буду учитывать в рекомендациях.")


@dp.message(Command("ivf_off"))
async def cmd_ivf_off(m: Message):
    n = db.end_states("ivf")
    await m.answer("Программа ЭКО завершена." if n else "Активной программы ЭКО не было записано.")


@dp.message(Command("injury"))
async def cmd_injury(m: Message, command: CommandObject):
    note = (command.args or "травма").strip()
    db.add_state("injury", note)
    await m.answer(f"Отметила травму: {note}. Учту в советах по активности.")


@dp.message(Command("state"))
async def cmd_state(m: Message):
    states = db.active_states()
    if not states:
        await m.answer("Активных особых состояний нет.")
        return
    lines = [f"• {s['kind']}: {s['description']} (с {s['started_at']})" for s in states]
    await m.answer("Активные состояния:\n" + "\n".join(lines))


@dp.message(Command("today"))
async def cmd_today(m: Message):
    today = date.today().isoformat()
    meals = db.meals_for_day(today)
    if not meals:
        await m.answer("Сегодня пока ничего не записано. Пришли фото еды 📷")
        return
    t = db.day_totals(today)
    listing = "\n".join(f"• {x['description']} (~{round(x['calories'] or 0)} ккал)" for x in meals)
    await m.answer(
        f"Сегодня:\n{listing}\n\n"
        f"Итого ≈ {t['calories']} ккал (Б {t['protein_g']} / Ж {t['fat_g']} / У {t['carbs_g']})"
    )


@dp.message(Command("summary"))
async def cmd_summary(m: Message):
    await m.answer("Считаю разбор дня…")
    text = await chat(prompts.daily_summary_prompt(), "Сделай разбор питания за сегодня.")
    await m.answer(text)


# ---------------- фото еды ----------------

@dp.message(F.photo)
async def on_photo(m: Message):
    await m.answer("Смотрю фото… 🍽")
    photo = m.photo[-1]
    file = await bot.get_file(photo.file_id)
    buf = await bot.download_file(file.file_path)
    data = buf.read()

    # сохраняем оригинал
    photo_path = config.PHOTOS_DIR / f"{photo.file_unique_id}.jpg"
    photo_path.write_bytes(data)

    caption = m.caption or "Определи блюдо и оцени КБЖУ."
    parsed = await chat_json(prompts.food_logging_prompt(), caption, image=data)

    if not parsed:
        await m.answer("Не получилось разобрать блюдо 😕 Попробуй другое фото или опиши словами.")
        return

    db.add_meal(parsed, photo_path=str(photo_path))
    reply = (
        f"📝 {parsed.get('description', 'блюдо')}\n"
        f"≈ {round(parsed.get('calories') or 0)} ккал | "
        f"Б {round(parsed.get('protein_g') or 0)} / "
        f"Ж {round(parsed.get('fat_g') or 0)} / "
        f"У {round(parsed.get('carbs_g') or 0)}"
    )
    if parsed.get("comment"):
        reply += f"\n\n💡 {parsed['comment']}"
    await m.answer(reply)


# ---------------- вопросы (любой другой текст) ----------------

_KIND_LABEL = {"illness": "болезнь", "ivf": "программа ЭКО", "injury": "травма", "other": "состояние"}


def apply_actions(actions: list) -> list[str]:
    """Применить распознанные действия к профилю. Вернуть подтверждения для пользователя."""
    notes: list[str] = []
    active_kinds = {s["kind"] for s in db.active_states()}
    for a in actions or []:
        t = (a.get("type") or "").strip()
        if t == "set_allergies":
            v = (a.get("value") or "").strip()
            if v:
                db.set_profile("allergies", v)
                notes.append(f"📝 Аллергии обновлены: {v}")
        elif t == "set_goal":
            v = (a.get("value") or "").strip()
            if v:
                db.set_profile("goal", v)
                notes.append(f"🎯 Цель обновлена: {v}")
        elif t == "add_state":
            kind = (a.get("kind") or "other").strip()
            desc = (a.get("description") or "").strip() or _KIND_LABEL.get(kind, kind)
            if kind in active_kinds:
                continue  # такое состояние уже активно — не дублируем
            db.add_state(kind, desc)
            active_kinds.add(kind)
            notes.append(f"🩺 Отмечено: {_KIND_LABEL.get(kind, kind)} — {desc}")
        elif t == "end_state":
            kind = (a.get("kind") or "").strip()
            if db.end_states(kind):
                notes.append(f"✅ Состояние закрыто: {_KIND_LABEL.get(kind, kind)}")
    return notes


@dp.message(F.text)
async def on_text(m: Message):
    data = await chat_json(prompts.router_prompt(), m.text)
    if not data or "reply" not in data:
        # не получили структуру — отвечаем обычным образом
        await m.answer(await chat(prompts.qa_prompt(), m.text))
        return
    notes = apply_actions(data.get("actions"))
    reply = (data.get("reply") or "Готово.").strip()
    if notes:
        reply += "\n\n" + "\n".join(notes)
    await m.answer(reply)


# ---------------- сводки по расписанию ----------------

async def send_daily_summary():
    today = date.today().isoformat()
    if not db.meals_for_day(today):
        return  # нечего разбирать
    text = await chat(prompts.daily_summary_prompt(), "Сделай разбор питания за сегодня.")
    await bot.send_message(config.OWNER_ID, "🌙 Разбор дня:\n\n" + text)


async def send_weekly_summary():
    end = date.today()
    start = end - timedelta(days=6)
    days = [(start + timedelta(days=i)).isoformat() for i in range(7)]
    per_day = []
    for d in days:
        t = db.day_totals(d)
        if t["count"]:
            per_day.append(f"{d}: ~{t['calories']} ккал, {t['count']} приём(а)")
    states = db.states_in_period(start.isoformat(), end.isoformat())
    states_txt = "\n".join(
        f"- {s['kind']}: {s['description']} ({s['started_at']}–{s['ended_at'] or 'сейчас'})"
        for s in states
    ) or "особых состояний не было"

    system = (
        prompts.BASE_PERSONA
        + "\n\n"
        + prompts._context_block()
        + "\nЗАДАЧА: дай недельную сводку и рекомендации. Питание по дням:\n"
        + ("\n".join(per_day) or "данных по питанию мало")
        + f"\n\nСостояния за неделю:\n{states_txt}\n\n"
        "Если была болезнь — учти, что спад активности и аппетита это объясняет, не ругай. "
        "Активность с часов (Garmin) подключим позже — пока опирайся на питание и состояния. "
        "6–10 строк: выводы + что улучшить на следующую неделю."
    )
    text = await chat(system, "Составь недельную сводку.")
    await bot.send_message(config.OWNER_ID, "📊 Итоги недели:\n\n" + text)


class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass  # не засорять логи


def start_health_server():
    """Мини HTTP-сервер для health-check Hugging Face Spaces."""
    port = int(os.getenv("PORT", "7860"))
    srv = HTTPServer(("0.0.0.0", port), _Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Health-check слушает порт %s", port)


def setup_scheduler():
    sched = AsyncIOScheduler(timezone=config.TIMEZONE)
    dh, dm = map(int, config.DAILY_SUMMARY_TIME.split(":"))
    sched.add_job(send_daily_summary, "cron", hour=dh, minute=dm)
    wh, wm = map(int, config.WEEKLY_SUMMARY_TIME.split(":"))
    sched.add_job(send_weekly_summary, "cron", day_of_week=config.WEEKLY_SUMMARY_DAY, hour=wh, minute=wm)

    async def _flush_job():
        await asyncio.to_thread(persistence.flush_if_dirty)

    sched.add_job(_flush_job, "interval", minutes=3)
    sched.start()
    log.info("Планировщик запущен: дневной разбор %s, недельный %s %s",
             config.DAILY_SUMMARY_TIME, config.WEEKLY_SUMMARY_DAY, config.WEEKLY_SUMMARY_TIME)


# ---------------- запуск ----------------

async def main():
    missing = config.check()
    if missing:
        raise SystemExit("Не заполнены поля в .env: " + ", ".join(missing))
    start_health_server()          # сразу открываем порт для health-check HF
    persistence.restore_on_boot()  # восстановить базу из бэкапа
    db.init()
    setup_scheduler()
    if not config.OWNER_ID:
        log.warning("OWNER_ID не задан — бот отвечает ВСЕМ. Отправь боту /id, впиши число в .env, перезапусти.")
    log.info("Бот запущен.")
    try:
        while True:
            try:
                await dp.start_polling(bot)
                break  # штатная остановка
            except TelegramNetworkError as e:
                log.warning("Сеть Telegram недоступна (%s) — повтор через 10 с", e)
                await asyncio.sleep(10)
    finally:
        await asyncio.to_thread(persistence.flush_if_dirty)  # сохранить базу при остановке


if __name__ == "__main__":
    asyncio.run(main())
