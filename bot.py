"""Telegram-бот: дневник питания + советы по запросу (Этап 1).

Запуск:  python bot.py
"""
import asyncio
import logging
import os
import socket
import threading
from collections import deque
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

# На некоторых хостингах (в т.ч. HF Spaces) IPv6 «висит» и api.telegram.org
# становится недоступен. Принудительно используем только IPv4.
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only(host, *args, **kwargs):
    res = _orig_getaddrinfo(host, *args, **kwargs)
    ipv4 = [r for r in res if r[0] == socket.AF_INET]
    return ipv4 or res


socket.getaddrinfo = _ipv4_only

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import db
import garmin_client
import persistence
import prompts
import llm
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
        "🧪 Пришли анализ (PDF или фото бланка) — распознаю и сохраню в историю.\n"
        "💬 Спроси что угодно — «что выбрать на обед?», «полезно ли это съесть?», "
        "«какая активность сегодня?» — отвечу с учётом твоих данных и анализов.\n\n"
        "Команды:\n"
        "/today — что съедено за день\n"
        "/labs — сводка по анализам\n"
        "/garmin — свежие данные с часов (сон, пульс, нагрузка)\n"
        "/notes — что я о тебе помню (привычки, предпочтения)\n"
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


@dp.message(Command("notes"))
async def cmd_notes(m: Message):
    notes = db.list_notes()
    if not notes:
        await m.answer("Заметок пока нет. Скажи «запомни, что…» — и я учту это в советах.")
        return
    lines = "\n".join(f"• {n['text']}" for n in notes)
    await m.answer("📌 Я помню о тебе:\n" + lines)


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
    today = config.today_local().isoformat()
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


@dp.message(Command("labs"))
async def cmd_labs(m: Message):
    ov = db.labs_overview()
    if not ov["count"]:
        await m.answer("Анализов пока нет. Пришли файл (PDF или фото бланка) — распознаю и сохраню.")
        return
    txt = (
        f"🧪 Анализы: {ov['count']} показателей за {ov['date_min']}–{ov['date_max']} "
        f"({ov['markers']} разных).\n\n"
    )
    if ov["abnormal"]:
        txt += "Последние вне нормы:\n" + "\n".join(
            f"{r['flag']} {r['name']}: {r['value']} {r['unit']} (норма {r['reference']}, {r['date']})"
            for r in ov["abnormal"][:30]
        )
    else:
        txt += "Свежих отклонений нет 👍"
    await m.answer(txt[:4000])


def _store_lab_doc(parsed: dict) -> tuple[int, list]:
    date = (parsed.get("date") or "").strip()
    lab = (parsed.get("lab") or "").strip()
    added, abn = 0, []
    for it in parsed.get("items", []) or []:
        row = {
            "date": date,
            "lab": lab,
            "category": it.get("category"),
            "name": it.get("name"),
            "value": it.get("value"),
            "unit": it.get("unit"),
            "reference": it.get("reference"),
            "flag": it.get("flag"),
        }
        if db.add_lab(row, source="telegram"):
            added += 1
            if (it.get("flag") or "").strip():
                abn.append(row)
    return added, abn


@dp.message(F.document)
async def on_document(m: Message):
    doc = m.document
    name = (doc.file_name or "").lower()
    is_pdf = (doc.mime_type == "application/pdf") or name.endswith(".pdf")
    is_img = (doc.mime_type or "").startswith("image/") or name.endswith((".jpg", ".jpeg", ".png", ".webp"))
    if not (is_pdf or is_img):
        await m.answer("Пришли анализ как PDF или фото — другие форматы пока не читаю.")
        return
    await m.answer("Читаю анализ… 🧪")
    file = await bot.get_file(doc.file_id)
    data = (await bot.download_file(file.file_path)).read()

    parsed = await llm.extract_labs(
        "Распознай показатели из этого анализа.",
        image=data if is_img else None,
        pdf=data if is_pdf else None,
    )
    if not parsed or not parsed.get("items"):
        await m.answer("Не получилось разобрать анализ 😕 Попробуй более чёткий скан/фото.")
        return
    added, abn = _store_lab_doc(parsed)
    reply = f"✅ Сохранила анализ от {parsed.get('date','?')}: {added} показателей."
    if abn:
        reply += "\n\nВне нормы:\n" + "\n".join(
            f"{r['flag']} {r['name']}: {r['value']} {r['unit']} (норма {r['reference']})" for r in abn[:15]
        )
    await m.answer(reply[:4000])


@dp.message(Command("garmin"))
async def cmd_garmin(m: Message):
    if not garmin_client.available():
        await m.answer(
            "Garmin ещё не подключён. Нужно один раз войти локально скриптом "
            "garmin_login.py — после этого пойдут данные по сну, пульсу и нагрузке."
        )
        return
    g = db.garmin_latest()
    if not g:
        await m.answer("Данные Garmin пока не собрались — загляни чуть позже.")
        return
    rows = [
        ("Сон", f"{g['sleep_hours']} ч" + (f" (оценка {g['sleep_score']})" if g.get("sleep_score") else "") if g.get("sleep_hours") else None),
        ("Готовность к нагрузке", f"{g['training_readiness']}/100" if g.get("training_readiness") is not None else None),
        ("Body Battery", g.get("body_battery")),
        ("Пульс покоя", g.get("resting_hr")),
        ("Стресс (сред.)", g.get("stress_avg")),
        ("Шаги", g.get("steps")),
        ("HRV", g.get("hrv")),
        ("VO2max", g.get("vo2max")),
    ]
    body = "\n".join(f"• {k}: {v}" for k, v in rows if v is not None)
    await m.answer(f"🟢 Garmin на {g['date']}:\n{body}")


@dp.message(Command("summary"))
async def cmd_summary(m: Message):
    await m.answer("Считаю разбор дня…")
    text = await chat(prompts.daily_summary_prompt(), "Сделай разбор питания за сегодня.")
    await m.answer(text)


# ---------------- фото еды ----------------

async def _handle_lab_image(m: Message, data: bytes):
    """Фото оказалось бланком анализов — распознаём как анализ."""
    await m.answer("Похоже на анализ — читаю показатели… 🧪")
    lab = await llm.extract_labs("Распознай показатели из этого анализа.", image=data)
    if not lab or not lab.get("items"):
        await m.answer("Не получилось разобрать анализ 😕 Пришли более чёткое фото или PDF.")
        return
    added, abn = _store_lab_doc(lab)
    reply = f"✅ Сохранила анализ от {lab.get('date', '?')}: {added} показателей."
    if abn:
        reply += "\n\nВне нормы:\n" + "\n".join(
            f"{r['flag']} {r['name']}: {r['value']} {r['unit']} (норма {r['reference']})" for r in abn[:15]
        )
    await m.answer(reply[:4000])


@dp.message(F.photo)
async def on_photo(m: Message):
    await m.answer("Смотрю фото… 👀")
    photo = m.photo[-1]
    file = await bot.get_file(photo.file_id)
    data = (await bot.download_file(file.file_path)).read()

    caption = m.caption or "Определи, что на фото."
    parsed = await chat_json(prompts.food_logging_prompt(), caption, image=data)
    if not parsed:
        await m.answer("Не получилось разобрать фото 😕 Попробуй другое или опиши словами.")
        return

    kind = (parsed.get("type") or "food").strip().lower()
    if kind == "lab":
        await _handle_lab_image(m, data)
        return
    if kind == "other":
        await m.answer("Хм, это не похоже ни на еду, ни на анализ. Если это блюдо — опиши словами, запишу.")
        return

    # еда — сохраняем оригинал и записываем в дневник
    photo_path = config.PHOTOS_DIR / f"{photo.file_unique_id}.jpg"
    photo_path.write_bytes(data)
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
        elif t == "add_note":
            txt = (a.get("text") or "").strip()
            if txt and db.add_note(txt):
                notes.append(f"📌 Запомнила: {txt}")
        elif t == "remove_note":
            if db.remove_note((a.get("match") or "").strip()):
                notes.append("🗑 Заметку убрала.")
        elif t == "add_meal":
            parsed = {
                k: a.get(k)
                for k in ("description", "calories", "protein_g", "fat_g", "carbs_g")
            }
            if (parsed.get("description") or "").strip():
                db.add_meal(parsed)
                notes.append(
                    f"🍽 Записала: {parsed['description']} (~{round(parsed.get('calories') or 0)} ккал)"
                )
        elif t == "delete_meal":
            old = db.delete_meal(a.get("meal_id"), (a.get("match") or "").strip())
            notes.append(f"🗑 Удалила запись: {old}" if old else "Не нашла запись для удаления.")
        elif t == "correct_meal":
            parsed = {
                k: a.get(k)
                for k in ("description", "calories", "protein_g", "fat_g", "carbs_g")
            }
            old = db.correct_meal(a.get("meal_id"), (a.get("match") or "").strip(), parsed)
            if old is not None:
                notes.append(f"✏️ Исправила «{old}» → «{parsed.get('description')}»")
            else:
                today_meals = db.meals_for_day(config.today_local().isoformat())
                lst = "\n".join(f"#{mm['id']}: {mm['description'][:60]}" for mm in today_meals) or "—"
                notes.append("⚠️ Не поняла, какую запись исправить. Уточни номер. Сегодня записано:\n" + lst)
    return notes


_GARMIN_TRIGGERS = (
    "body battery", "боди бат", "боди бет", "батар", "garmin", "гармин",
    "с часов", "на часах", "готовность", "vo2", "восстанов",
    "обнови", "актуальн", "свеж",
)


_history = deque(maxlen=10)  # последние реплики (роль, текст) — память диалога


def _with_history(text: str) -> str:
    if not _history:
        return "СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЯ: " + text
    convo = "\n".join(
        f"{'Пользователь' if r == 'user' else 'Ты (бот)'}: {t}" for r, t in _history
    )
    return (
        "НЕДАВНЯЯ ПЕРЕПИСКА (ссылки «это», «тот же», «он» относятся сюда; "
        "не переспрашивай то, что уже понятно из контекста):\n"
        f"{convo}\n\nНОВОЕ СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЯ: {text}"
    )


@dp.message(F.text)
async def on_text(m: Message):
    txt = m.text.lower()
    if garmin_client.available() and any(t in txt for t in _GARMIN_TRIGGERS):
        await pull_garmin_today()  # свежие данные с часов перед ответом
    user_content = _with_history(m.text)
    data = await chat_json(prompts.router_prompt(), user_content)
    if not data or "reply" not in data:
        # не получили структуру — отвечаем обычным образом
        reply = await chat(prompts.qa_prompt(), user_content)
        await m.answer(reply)
        _history.append(("user", m.text))
        _history.append(("bot", reply[:300]))
        return
    notes = apply_actions(data.get("actions"))
    reply = (data.get("reply") or "Готово.").strip()
    _history.append(("user", m.text))
    _history.append(("bot", reply[:300]))
    if notes:
        reply += "\n\n" + "\n".join(notes)
    await m.answer(reply)


# ---------------- сводки по расписанию ----------------

async def send_daily_summary():
    # Если сводка приходит ночью/рано утром — разбираем ВЧЕРАШНИЙ (завершённый) день.
    now = config.now_local()
    target = config.today_local() if now.hour >= 6 else (config.today_local() - timedelta(days=1))
    day = target.isoformat()
    if not db.meals_for_day(day):
        return  # нечего разбирать
    text = await chat(prompts.daily_summary_prompt(day), f"Сделай разбор питания за {day}.")
    await bot.send_message(config.OWNER_ID, f"🌙 Разбор дня ({day}):\n\n" + text)


async def send_weekly_summary():
    end = config.today_local() - timedelta(days=1)  # по вчера включительно (завершённая неделя)
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

    gdays = db.garmin_range(start.isoformat(), end.isoformat())
    if gdays:
        def avg(field):
            xs = [g[field] for g in gdays if g.get(field) is not None]
            return round(sum(xs) / len(xs), 1) if xs else None
        garmin_txt = (
            f"Garmin за неделю (среднее): сон {avg('sleep_hours')} ч, "
            f"пульс покоя {avg('resting_hr')}, "
            f"стресс {avg('stress_avg')}, шаги {avg('steps')}, "
            f"body battery {avg('body_battery')}, VO2max {avg('vo2max')}."
        )
    else:
        garmin_txt = "Данных Garmin за неделю нет."

    acts = db.garmin_activities_range(start.isoformat(), end.isoformat())
    if acts:
        garmin_txt += "\nТренировки за неделю:\n" + "\n".join(
            f"- {a['date']} {garmin_client.type_ru(a.get('type'))}: "
            + ", ".join(filter(None, [
                f"{a['distance_km']} км" if a.get("distance_km") else None,
                f"{a['duration_min']} мин" if a.get("duration_min") else None,
            ]))
            for a in acts
        )

    system = (
        prompts.BASE_PERSONA
        + "\n\n"
        + prompts._context_block()
        + "\nЗАДАЧА: дай недельную сводку и рекомендации. Питание по дням:\n"
        + ("\n".join(per_day) or "данных по питанию мало")
        + f"\n\nСостояния за неделю:\n{states_txt}\n\n"
        + garmin_txt
        + "\n\nЕсли была болезнь — учти, что спад активности и аппетита это объясняет, не ругай. "
        "Свяжи питание, сон и нагрузку с самочувствием и целью (здоровье + умеренная выносливость). "
        "6–10 строк: выводы + что улучшить на следующую неделю по еде и по активности."
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
    """Мини HTTP-сервер для health-check (HF Spaces / Render)."""
    port = int(os.getenv("PORT", "7860"))
    srv = HTTPServer(("0.0.0.0", port), _Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Health-check слушает порт %s", port)


async def keepalive():
    """Самопинг своего публичного адреса, чтобы бесплатный Render не засыпал."""
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        return
    import aiohttp

    while True:
        await asyncio.sleep(600)  # каждые 10 минут
        try:
            async with aiohttp.ClientSession() as s:
                await s.get(url, timeout=aiohttp.ClientTimeout(total=20))
        except Exception as e:
            log.warning("keepalive ping не прошёл: %s", e)


async def pull_garmin(days: int = 3):
    """Забрать метрики Garmin за последние дни (фоном, не блокируя бота)."""
    if not garmin_client.available():
        garmin_client.restore_tokens()  # вдруг токены уже залиты после входа
    if not garmin_client.available():
        return

    def work():
        garmin_client.sync_tokens()  # взять свежий токен из датасета (обновляет Pi)
        c = garmin_client.client()
        for i in range(days):
            d = (config.today_local() - timedelta(days=i)).isoformat()
            try:
                db.add_garmin_day(garmin_client.fetch_day(c, d))
            except Exception as e:
                log.warning("Garmin %s: %s", d, e)
        # тренировки за последнюю неделю (заплывы, бег и т.п.)
        start = (config.today_local() - timedelta(days=max(days, 7))).isoformat()
        for a in garmin_client.fetch_activities(c, start, config.today_local().isoformat()):
            db.add_garmin_activity(a)

    try:
        await asyncio.to_thread(work)
        log.info("Данные Garmin обновлены.")
    except Exception as e:
        log.warning("Не удалось обновить Garmin: %s", e)


_last_garmin_refresh = 0.0


async def pull_garmin_today() -> bool:
    """Быстро обновить ТОЛЬКО сегодняшние показатели (для свежести по запросу)."""
    global _last_garmin_refresh
    if not garmin_client.available():
        garmin_client.restore_tokens()
    if not garmin_client.available():
        return False
    import time
    if time.monotonic() - _last_garmin_refresh < 300:
        return True  # обновляли недавно — данные уже свежие
    _last_garmin_refresh = time.monotonic()

    def work():
        garmin_client.sync_tokens()  # взять свежий токен из датасета (обновляет Pi)
        c = garmin_client.client()
        today = config.today_local()
        # добираем окно последних 3 дней — чтобы не терять поздно синхронизированные
        # данные и активности (например, тренировку, залитую с часов позже)
        for i in range(3):
            db.add_garmin_day(garmin_client.fetch_day(c, (today - timedelta(days=i)).isoformat()))
        start = (today - timedelta(days=3)).isoformat()
        for a in garmin_client.fetch_activities(c, start, today.isoformat()):
            db.add_garmin_activity(a)

    try:
        await asyncio.to_thread(work)
        return True
    except Exception as e:
        log.warning("Garmin refresh: %s", e)
        return False


def setup_scheduler():
    sched = AsyncIOScheduler(timezone=config.TIMEZONE)
    sched.add_job(pull_garmin, "cron", hour=9, minute=15)  # утренний полный сбор (3 дня + неделя)
    sched.add_job(pull_garmin_today, "interval", hours=1)  # ежечасное обновление «на сейчас»
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
    start_health_server()          # сразу открываем порт для health-check
    persistence.restore_on_boot()  # восстановить базу из бэкапа
    db.init()
    garmin_client.restore_tokens()  # подтянуть токены Garmin (если есть)
    setup_scheduler()
    asyncio.create_task(pull_garmin())  # стартовый сбор Garmin
    if not config.OWNER_ID:
        log.warning("OWNER_ID не задан — бот отвечает ВСЕМ. Отправь боту /id, впиши число в .env, перезапусти.")
    asyncio.create_task(keepalive())
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
