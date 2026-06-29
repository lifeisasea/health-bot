"""Сборка системных подсказок (промптов) с учётом профиля и состояний."""
from datetime import date

import config
import db

GOAL_DEFAULT = "Поддержание здоровья и умеренной выносливости."

DISCLAIMER = (
    "Ты информационный помощник, а не врач. Ты не ставишь диагнозы и не назначаешь "
    "лечение. По медицинским вопросам и при тревожных показателях советуй обратиться "
    "к врачу."
)


def _context_block() -> str:
    """Постоянный контекст пользователя — кэшируется MiniMax между запросами."""
    goal = db.get_profile("goal", GOAL_DEFAULT)
    allergies = db.get_profile("allergies", "не указаны")

    states = db.active_states()
    if states:
        lines = []
        for s in states:
            label = {
                "illness": "болезнь",
                "ivf": "программа ЭКО",
                "injury": "травма",
                "other": "состояние",
            }.get(s["kind"], s["kind"])
            lines.append(f"- {label}: {s['description']} (с {s['started_at']})")
        states_txt = "\n".join(lines)
    else:
        states_txt = "нет активных особых состояний"

    return (
        f"СЕГОДНЯШНЯЯ ДАТА: {config.today_local().isoformat()} "
        "(данные Garmin могут быть от более раннего дня — это не «сегодня», "
        "ориентируйся на дату рядом с показателем).\n"
        f"ЦЕЛЬ ПОЛЬЗОВАТЕЛЯ: {goal}\n"
        f"АЛЛЕРГИИ (учитывать всегда): {allergies}\n"
        f"АКТИВНЫЕ СОСТОЯНИЯ ЗДОРОВЬЯ:\n{states_txt}\n"
        f"{_notes_block()}"
        f"{_garmin_block()}"
        f"{_labs_block()}"
    )


def _notes_block() -> str:
    """Произвольные заметки/привычки, которые пользователь просил запомнить."""
    notes = db.list_notes()
    if not notes:
        return ""
    lines = "\n".join(f"- {n['text']}" for n in notes)
    return f"ЗАМЕТКИ О ПОЛЬЗОВАТЕЛЕ (всегда учитывай в советах):\n{lines}\n"


def _garmin_block() -> str:
    """Метрики Garmin: сегодня + неделя + последние тренировки."""
    import garmin_client
    from datetime import date, timedelta

    g = db.garmin_latest()
    week = db.garmin_range((config.today_local() - timedelta(days=7)).isoformat(), config.today_local().isoformat())
    acts = db.garmin_activities_range((config.today_local() - timedelta(days=7)).isoformat(), config.today_local().isoformat())
    if not g and not week and not acts:
        return ""

    out = []
    if g:
        p = []
        if g.get("sleep_hours"):
            p.append(f"сон {g['sleep_hours']} ч" + (f" (оценка {g['sleep_score']})" if g.get("sleep_score") else ""))
        if g.get("training_readiness") is not None:
            p.append(f"готовность {g['training_readiness']}/100")
        if g.get("body_battery") is not None:
            p.append(f"body battery {g['body_battery']}")
        if g.get("resting_hr"):
            p.append(f"пульс покоя {g['resting_hr']}")
        if g.get("stress_avg") is not None:
            p.append(f"стресс {g['stress_avg']}")
        if g.get("steps"):
            p.append(f"шаги {g['steps']}")
        if g.get("hrv"):
            p.append(f"HRV {g['hrv']}")
        if g.get("vo2max"):
            p.append(f"VO2max {g['vo2max']}")
        if p:
            out.append(f"ДАННЫЕ GARMIN (на {g['date']}): " + ", ".join(p) + ".")

    if week:
        def avg(f):
            xs = [d[f] for d in week if d.get(f) is not None]
            return round(sum(xs) / len(xs), 1) if xs else None
        out.append(
            f"За 7 дней (среднее): сон {avg('sleep_hours')} ч, шаги {avg('steps')}, "
            f"пульс покоя {avg('resting_hr')}, стресс {avg('stress_avg')}."
        )

    if acts:
        lines = []
        for a in acts[:12]:
            parts = [garmin_client.type_ru(a.get("type"))]
            if a.get("distance_km"):
                parts.append(f"{a['distance_km']} км")
            if a.get("duration_min"):
                parts.append(f"{a['duration_min']} мин")
            if a.get("calories"):
                parts.append(f"{round(a['calories'])} ккал")
            lines.append(f"  {a['date']}: " + ", ".join(parts))
        out.append("Тренировки за 7 дней:\n" + "\n".join(lines))
    else:
        out.append("Отдельных тренировок за 7 дней в Garmin не вижу.")

    out.append(
        "Учитывай это при советах по активности: низкая готовность/плохой сон/высокий стресс → "
        "лёгкая нагрузка или отдых; хорошее восстановление → можно интенсивнее. "
        "Если пользователь упоминает тренировку — сверься со списком выше, прежде чем говорить, что её не видишь."
    )
    return "\n".join(out) + "\n"


def _labs_block() -> str:
    """Компактная сводка анализов для контекста: отклонения + охват."""
    ov = db.labs_overview()
    if not ov["count"]:
        return ""
    lines = [
        f"АНАЛИЗЫ: в истории {ov['count']} показателей "
        f"({ov['date_min']}–{ov['date_max']}), {ov['markers']} разных."
    ]
    if ov["abnormal"]:
        lines.append("Последние значения ВНЕ нормы (учитывай в рекомендациях по питанию/образу жизни):")
        for r in ov["abnormal"][:25]:
            arrow = r["flag"]
            lines.append(
                f"  {arrow} {r['name']}: {r['value']} {r['unit']} "
                f"(норма {r['reference']}; {r['date']})"
            )
    else:
        lines.append("Свежих отклонений нет.")
    lines.append(
        "Это данные для контекста, а не диагноз. Можешь ссылаться на конкретный показатель, "
        "но при тревожных значениях советуй обсудить с врачом."
    )
    return "\n".join(lines) + "\n"


BASE_PERSONA = (
    "Ты — персональный помощник по питанию, активности и здоровью. "
    "Общайся по-русски, тепло, кратко и конкретно, без воды. "
    "Всегда учитывай аллергии, цель и активные состояния здоровья пользователя. "
    "Не выдумывай данные, которых нет в контексте: если про съеденное сказано "
    "«пока ничего не записано» — не упоминай несуществующие блюда. "
    + DISCLAIMER
)


def food_logging_prompt() -> str:
    """Для распознавания фото еды. Просим строгий JSON."""
    return (
        BASE_PERSONA
        + "\n\n"
        + _context_block()
        + "\nЗАДАЧА: по фото определи блюдо и оцени пищевую ценность. "
        "Оценки приблизительные — это нормально. Ответь ТОЛЬКО валидным JSON без "
        "пояснений в формате:\n"
        '{"description": "краткое название блюда и порции", '
        '"items": ["ингредиент1", "ингредиент2"], '
        '"calories": число_ккал, "protein_g": белки, "fat_g": жиры, '
        '"carbs_g": углеводы, "confidence": "low|medium|high", '
        '"comment": "одно короткое замечание с учётом аллергий и цели"}'
    )


def lab_extraction_prompt() -> str:
    """Извлечение показателей из файла анализов (фото/PDF). Строгий JSON."""
    return (
        "Ты медицинский ассистент по распознаванию бланков анализов. "
        "Извлеки ВСЕ показатели из документа максимально точно. "
        "Дату бери из бланка (формат YYYY-MM-DD). Значение сохраняй как в бланке "
        "(включая '<', '>'). Флаг: '↑' если выше нормы, '↓' если ниже, иначе пусто. "
        "Ответь ТОЛЬКО валидным JSON без пояснений:\n"
        '{"date": "YYYY-MM-DD", "lab": "лаборатория или пусто", '
        '"items": [{"category": "раздел", "name": "показатель", "value": "результат", '
        '"unit": "ед", "reference": "референс", "flag": "↑|↓|"}]}'
    )


def qa_prompt() -> str:
    """Для ответов на вопросы пользователя (что съесть, чем заняться и т.п.)."""
    today = config.today_local().isoformat()
    totals = db.day_totals(today)
    meals = db.meals_for_day(today)
    eaten = "; ".join(m["description"] for m in meals) or "пока ничего не записано"
    food_today = (
        f"СЕГОДНЯ СЪЕДЕНО: {eaten}. "
        f"Итого за день ≈ {totals['calories']} ккал "
        f"(Б {totals['protein_g']} / Ж {totals['fat_g']} / У {totals['carbs_g']})."
    )
    return (
        BASE_PERSONA
        + "\n\n"
        + _context_block()
        + food_today
        + "\n\nОтветь на вопрос пользователя по существу, опираясь на эти данные. "
        "Если советуешь еду — проверь по аллергиям. Если спрашивают про активность — "
        "учитывай цель (умеренная выносливость) и активные состояния."
    )


def router_prompt() -> str:
    """Понимание свободной речи: ответить И при необходимости обновить профиль."""
    today = config.today_local().isoformat()
    totals = db.day_totals(today)
    meals = db.meals_for_day(today)
    recent = db.recent_meals(2)
    eaten = "; ".join(f"[#{m['id']} {m['day'][5:]}] {m['description']}" for m in recent) or "пока ничего не записано"
    allergies = db.get_profile("allergies", "не указаны")
    return (
        BASE_PERSONA
        + "\n\n"
        + _context_block()
        + f"ПОСЛЕДНИЕ ЗАПИСИ ЕДЫ (формат [#номер дата]; на номер ссылайся при исправлении): {eaten}. "
        f"Итого за сегодня ≈ {totals['calories']} ккал "
        f"(Б {totals['protein_g']} / Ж {totals['fat_g']} / У {totals['carbs_g']}).\n\n"
        "Пользователь пишет обычным текстом. Ты ОДНОВРЕМЕННО делаешь две вещи:\n"
        "1) отвечаешь по существу (поле reply), опираясь на данные выше;\n"
        "2) если из сообщения следует изменение профиля — фиксируешь это в actions.\n\n"
        "Когда добавлять действие в actions:\n"
        "- пользователь называет аллергию/непереносимость → "
        f'{{"type":"set_allergies","value":"<ПОЛНЫЙ обновлённый список, объедини с текущими: {allergies}>"}}\n'
        '- заболел(а), простуда, температура, плохое самочувствие → {"type":"add_state","kind":"illness","description":"<что именно>"}\n'
        '- выздоровел(а)/поправилась → {"type":"end_state","kind":"illness"}\n'
        '- началась программа ЭКО → {"type":"add_state","kind":"ivf","description":"<деталь>"}; завершилась → {"type":"end_state","kind":"ivf"}\n'
        '- травма → {"type":"add_state","kind":"injury","description":"<деталь>"}\n'
        '- хочет изменить цель → {"type":"set_goal","value":"<новая цель>"}\n'
        '- просит ЗАПОМНИТЬ произвольный факт/привычку/предпочтение ("запомни, что…", '
        '"учитывай, что…", "я всегда…", "у меня привычка…", "обычно я…") → '
        '{"type":"add_note","text":"<кратко суть факта, от первого лица>"}\n'
        '- просит ЗАБЫТЬ/удалить заметку ("забудь, что…", "это уже неактуально") → '
        '{"type":"remove_note","match":"<ключевые слова заметки>"}\n'
        '- СООБЩАЕТ, что съел/выпил что-то (факт, прошедшее время: «выпила латте», «съела банан») → '
        '{"type":"add_meal","description":"<блюдо и порция>","calories":N,"protein_g":N,"fat_g":N,"carbs_g":N} '
        "— оцени КБЖУ. НЕ добавляй, если это вопрос/гипотеза («можно ли…», «что если…») "
        "или если это блюдо УЖЕ есть в списке последних записей выше (не дублируй).\n"
        '- просит УДАЛИТЬ запись еды («удали», «убери запись», «это лишнее») → '
        '{"type":"delete_meal","meal_id":<#номер из списка>,"match":"<слово>"}\n'
        '- ПОПРАВЛЯЕТ распознанную еду (например «на завтрак был лосось, а не помидоры») → '
        '{"type":"correct_meal","meal_id":<номер # из списка СЕГОДНЯ СЪЕДЕНО, к которому относится правка>,'
        '"match":"<ошибочное слово, напр. помидор>",'
        '"description":"<полное исправленное блюдо>","calories":N,"protein_g":N,"fat_g":N,"carbs_g":N} '
        "— ОБЯЗАТЕЛЬНО укажи meal_id той самой записи (сверься со списком и словами пользователя про приём пищи), "
        "пересчитай КБЖУ. Если непонятно, к какой записи относится правка — НЕ угадывай, попроси уточнить.\n"
        "Если менять профиль/еду не нужно — actions: []. Не выдумывай состояния без явных слов пользователя.\n"
        "Простой вопрос про еду/активность/самочувствие — это просто reply без действий.\n\n"
        "Ответь ТОЛЬКО валидным JSON, без пояснений:\n"
        '{"reply": "<текст пользователю>", "actions": [<0 или более действий>]}'
    )


def daily_summary_prompt(day: str = None) -> str:
    today = day or config.today_local().isoformat()
    totals = db.day_totals(today)
    meals = db.meals_for_day(today)
    listing = "\n".join(
        f"- {m['description']} (~{round(m['calories'] or 0)} ккал)" for m in meals
    ) or "за день не записано ни одного приёма пищи"
    return (
        BASE_PERSONA
        + "\n\n"
        + _context_block()
        + f"\nПИТАНИЕ ЗА ДЕНЬ ({today}):\n{listing}\n"
        f"Итого ≈ {totals['calories']} ккал "
        f"(Б {totals['protein_g']} / Ж {totals['fat_g']} / У {totals['carbs_g']}).\n\n"
        "ЗАДАЧА: дай короткий дружелюбный разбор дня по питанию: что хорошо, что не очень "
        "и что улучшить завтра. 4–6 строк, по делу. Учитывай аллергии и цель."
    )
