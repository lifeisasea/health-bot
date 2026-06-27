"""Хранилище на SQLite: профиль, состояния здоровья, приёмы пищи."""
import json
import sqlite3
from datetime import datetime, date, timedelta
from typing import Optional

import persistence
from config import DB_PATH


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS profile (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS health_states (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                kind        TEXT,            -- illness | ivf | injury | other
                description TEXT,
                started_at  TEXT,           -- ISO date
                ended_at    TEXT            -- ISO date или NULL, если активно
            );

            CREATE TABLE IF NOT EXISTS meals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT,           -- ISO datetime
                day         TEXT,           -- ISO date (для группировки)
                description TEXT,
                calories    REAL,
                protein_g   REAL,
                fat_g       REAL,
                carbs_g     REAL,
                photo_path  TEXT,
                raw         TEXT            -- полный JSON от модели
            );

            CREATE TABLE IF NOT EXISTS labs (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                date      TEXT,             -- ISO date анализа
                lab       TEXT,             -- лаборатория
                category  TEXT,
                name      TEXT,             -- показатель
                value     TEXT,             -- результат (текст: бывает "<3.0")
                unit      TEXT,
                reference TEXT,
                flag      TEXT,             -- ↑ / ↓ / пусто
                source    TEXT,             -- import:xlsx | telegram
                added_at  TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_labs_name_date ON labs(name, date);

            CREATE TABLE IF NOT EXISTS garmin_daily (
                date               TEXT PRIMARY KEY,
                steps              INTEGER,
                resting_hr         INTEGER,
                stress_avg         INTEGER,
                body_battery       INTEGER,
                sleep_hours        REAL,
                sleep_score        INTEGER,
                training_readiness INTEGER,
                vo2max             REAL,
                hrv                INTEGER,
                updated_at         TEXT
            );

            CREATE TABLE IF NOT EXISTS garmin_activities (
                activity_id  INTEGER PRIMARY KEY,
                date         TEXT,
                type         TEXT,
                name         TEXT,
                duration_min REAL,
                distance_km  REAL,
                calories     REAL,
                added_at     TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_gact_date ON garmin_activities(date);
            """
        )


# ---------- профиль (ключ-значение) ----------

def set_profile(key: str, value: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO profile(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
    persistence.mark_dirty()


def get_profile(key: str, default: str = "") -> str:
    with _conn() as c:
        row = c.execute("SELECT value FROM profile WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


# ---------- состояния здоровья ----------

def add_state(kind: str, description: str, started_at: Optional[str] = None) -> int:
    started_at = started_at or date.today().isoformat()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO health_states(kind, description, started_at, ended_at) "
            "VALUES(?, ?, ?, NULL)",
            (kind, description, started_at),
        )
        persistence.mark_dirty()
        return cur.lastrowid


def end_states(kind: str, ended_at: Optional[str] = None) -> int:
    """Закрыть все активные состояния данного типа. Вернуть число закрытых."""
    ended_at = ended_at or date.today().isoformat()
    with _conn() as c:
        cur = c.execute(
            "UPDATE health_states SET ended_at=? WHERE kind=? AND ended_at IS NULL",
            (ended_at, kind),
        )
        persistence.mark_dirty()
        return cur.rowcount


def active_states() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM health_states WHERE ended_at IS NULL ORDER BY started_at"
        ).fetchall()
        return [dict(r) for r in rows]


def states_in_period(start_day: str, end_day: str) -> list[dict]:
    """Состояния, пересекающиеся с периодом [start_day, end_day]."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM health_states "
            "WHERE started_at <= ? AND (ended_at IS NULL OR ended_at >= ?) "
            "ORDER BY started_at",
            (end_day, start_day),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- приёмы пищи ----------

def add_meal(parsed: dict, photo_path: Optional[str] = None) -> int:
    now = datetime.now()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO meals(ts, day, description, calories, protein_g, fat_g, "
            "carbs_g, photo_path, raw) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                now.isoformat(timespec="seconds"),
                now.date().isoformat(),
                parsed.get("description", ""),
                parsed.get("calories"),
                parsed.get("protein_g"),
                parsed.get("fat_g"),
                parsed.get("carbs_g"),
                photo_path,
                json.dumps(parsed, ensure_ascii=False),
            ),
        )
        persistence.mark_dirty()
        return cur.lastrowid


def correct_meal(meal_id, match: str, parsed: dict) -> Optional[str]:
    """Исправить сегодняшнюю запись еды строго по номеру (meal_id) или по слову (match).
    БЕЗ затирания «последней»: если ничего не нашли — вернуть None (бот переспросит).
    Ищем среди недавних записей (последние 7 дней), а не только сегодня.
    Вернуть старое описание при успехе."""
    floor = (date.today() - timedelta(days=7)).isoformat()
    with _conn() as c:
        row = None
        if meal_id:
            row = c.execute(
                "SELECT * FROM meals WHERE id=? AND day>=?", (meal_id, floor)
            ).fetchone()
        if not row and match:
            row = c.execute(
                "SELECT * FROM meals WHERE day>=? AND description LIKE ? ORDER BY ts DESC LIMIT 1",
                (floor, f"%{match}%"),
            ).fetchone()
        if not row:
            return None
        old = row["description"]
        c.execute(
            "UPDATE meals SET description=?, calories=?, protein_g=?, fat_g=?, carbs_g=?, raw=? "
            "WHERE id=?",
            (
                parsed.get("description", old),
                parsed.get("calories"),
                parsed.get("protein_g"),
                parsed.get("fat_g"),
                parsed.get("carbs_g"),
                json.dumps(parsed, ensure_ascii=False),
                row["id"],
            ),
        )
    persistence.mark_dirty()
    return old


def repair_20260627() -> None:
    """Разовый ремонт: исправление от 27.06 уехало не в ту запись (затёрло ужин).
    Идемпотентно (срабатывает только при наличии ошибочного состояния)."""
    bf = "Тост с крем-сыром и лососем + тост с крем-сыром, чёрный чай с 2 ч.л. коричневого сахара"
    dn = ("Куриная печень тушёная с картофелем фри из аэрогриля, греческим йогуртом 2%, "
          "хлебом sourdough и ягодным соком 50/50 с водой (3 стакана)")
    changed = False
    with _conn() as c:
        # ужин (id9) был затёрт текстом тоста с лососем — восстановить
        r = c.execute(
            "SELECT id FROM meals WHERE id=9 AND day='2026-06-27' "
            "AND description LIKE '%лосос%' AND description LIKE '%крем-сыр%'"
        ).fetchone()
        if r:
            c.execute(
                "UPDATE meals SET description=?, calories=1050, protein_g=55, fat_g=40, carbs_g=120 "
                "WHERE id=9",
                (dn,),
            )
            changed = True
        # завтрак (id4): помидоры -> лосось
        r = c.execute(
            "SELECT id FROM meals WHERE id=4 AND day='2026-06-27' "
            "AND description LIKE '%помидор%'"
        ).fetchone()
        if r:
            c.execute(
                "UPDATE meals SET description=?, calories=435, protein_g=17, fat_g=23, carbs_g=43 "
                "WHERE id=4",
                (bf,),
            )
            changed = True
        # бургер (id5): маленький слайдер -> обычный бургер
        burger = ("Обычный бургер на булочке бриошь с говяжьей котлетой без соуса и овощей "
                  "+ порция картофеля фри + лимонад с клубникой и базиликом (Hard Rock Cafe Dubai)")
        r = c.execute(
            "SELECT id FROM meals WHERE id=5 AND day='2026-06-27' AND description LIKE '%Маленький бургер%'"
        ).fetchone()
        if r:
            c.execute(
                "UPDATE meals SET description=?, calories=1180, protein_g=35, fat_g=54, carbs_g=130 "
                "WHERE id=5",
                (burger,),
            )
            changed = True
        # мороженое (id6): убрать взбитые сливки и сироп
        sundae = ("Шоколадно-ванильный сандей в стакане (шарик ванильного и шарик шоколадного "
                  "мороженого, без взбитых сливок и сиропа)")
        r = c.execute(
            "SELECT id FROM meals WHERE id=6 AND day='2026-06-27' AND description LIKE '%взбитыми сливками%'"
        ).fetchone()
        if r:
            c.execute(
                "UPDATE meals SET description=?, calories=290, protein_g=5, fat_g=15, carbs_g=33 "
                "WHERE id=6",
                (sundae,),
            )
            changed = True
    if changed:
        persistence.mark_dirty()


def meals_for_day(day: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM meals WHERE day=? ORDER BY ts", (day,)
        ).fetchall()
        return [dict(r) for r in rows]


def recent_meals(days: int = 2) -> list[dict]:
    """Записи еды за последние N дней (для исправлений по номеру)."""
    floor = (date.today() - timedelta(days=days)).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM meals WHERE day>=? ORDER BY ts", (floor,)
        ).fetchall()
        return [dict(r) for r in rows]


def day_totals(day: str) -> dict:
    meals = meals_for_day(day)
    return {
        "count": len(meals),
        "calories": round(sum(m["calories"] or 0 for m in meals)),
        "protein_g": round(sum(m["protein_g"] or 0 for m in meals)),
        "fat_g": round(sum(m["fat_g"] or 0 for m in meals)),
        "carbs_g": round(sum(m["carbs_g"] or 0 for m in meals)),
    }


# ---------- анализы ----------

def add_lab(row: dict, source: str = "telegram", dedup: bool = True) -> bool:
    """Добавить один показатель анализа. Вернуть True, если вставлено (False — дубль)."""
    date = (row.get("date") or "").strip()
    name = (row.get("name") or "").strip()
    value = ("" if row.get("value") is None else str(row.get("value"))).strip()
    if not name:
        return False
    with _conn() as c:
        if dedup:
            dup = c.execute(
                "SELECT 1 FROM labs WHERE date=? AND name=? AND value=?",
                (date, name, value),
            ).fetchone()
            if dup:
                return False
        c.execute(
            "INSERT INTO labs(date, lab, category, name, value, unit, reference, flag, "
            "source, added_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                date,
                (row.get("lab") or "").strip(),
                (row.get("category") or "").strip(),
                name,
                value,
                (row.get("unit") or "").strip(),
                (row.get("reference") or "").strip(),
                (row.get("flag") or "").strip(),
                source,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
    persistence.mark_dirty()
    return True


def labs_count() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM labs").fetchone()["n"]


def latest_per_marker() -> list[dict]:
    """Последнее значение по каждому показателю (по самой свежей дате)."""
    with _conn() as c:
        rows = c.execute(
            "SELECT t.* FROM labs t JOIN ("
            "  SELECT name, MAX(date) AS md FROM labs GROUP BY name"
            ") m ON t.name=m.name AND t.date=m.md ORDER BY t.category, t.name"
        ).fetchall()
        # на одну дату может быть несколько строк одного имени — берём по одной
        seen, out = set(), []
        for r in rows:
            if r["name"] in seen:
                continue
            seen.add(r["name"])
            out.append(dict(r))
        return out


def labs_overview() -> dict:
    """Сводка для контекста: диапазон дат, кол-во, последние отклонения (с флагом)."""
    latest = latest_per_marker()
    with _conn() as c:
        rng = c.execute("SELECT MIN(date) AS lo, MAX(date) AS hi FROM labs").fetchone()
    abnormal = [r for r in latest if r["flag"]]
    return {
        "count": labs_count(),
        "markers": len(latest),
        "date_min": rng["lo"] if rng else None,
        "date_max": rng["hi"] if rng else None,
        "abnormal": abnormal,
        "latest": latest,
    }


def marker_history(name: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM labs WHERE name=? ORDER BY date", (name,)
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- Garmin ----------

_GARMIN_FIELDS = [
    "steps", "resting_hr", "stress_avg", "body_battery",
    "sleep_hours", "sleep_score", "training_readiness", "vo2max", "hrv",
]


def add_garmin_day(d: dict) -> None:
    """Сохранить/обновить метрики за день (upsert по дате)."""
    date = (d.get("date") or "").strip()
    if not date:
        return
    cols = ["date"] + _GARMIN_FIELDS + ["updated_at"]
    vals = [date] + [d.get(f) for f in _GARMIN_FIELDS] + [
        datetime.now().isoformat(timespec="seconds")
    ]
    # COALESCE: новое пустое значение не затирает уже сохранённое
    upd = ", ".join(f"{f}=COALESCE(excluded.{f}, garmin_daily.{f})" for f in _GARMIN_FIELDS)
    upd += ", updated_at=excluded.updated_at"
    with _conn() as c:
        c.execute(
            f"INSERT INTO garmin_daily({', '.join(cols)}) "
            f"VALUES({', '.join('?' * len(cols))}) "
            f"ON CONFLICT(date) DO UPDATE SET {upd}",
            vals,
        )
    persistence.mark_dirty()


def garmin_latest() -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM garmin_daily WHERE steps IS NOT NULL OR sleep_hours IS NOT NULL "
            "ORDER BY date DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def garmin_range(start_day: str, end_day: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM garmin_daily WHERE date BETWEEN ? AND ? ORDER BY date",
            (start_day, end_day),
        ).fetchall()
        return [dict(r) for r in rows]


def add_garmin_activity(a: dict) -> None:
    aid = a.get("activity_id")
    if not aid:
        return
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO garmin_activities(activity_id, date, type, name, "
            "duration_min, distance_km, calories, added_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                aid,
                (a.get("date") or "").strip(),
                a.get("type"),
                a.get("name"),
                a.get("duration_min"),
                a.get("distance_km"),
                a.get("calories"),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
    persistence.mark_dirty()


def garmin_activities_range(start_day: str, end_day: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM garmin_activities WHERE date BETWEEN ? AND ? ORDER BY date DESC",
            (start_day, end_day),
        ).fetchall()
        return [dict(r) for r in rows]
