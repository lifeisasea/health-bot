"""Хранилище на SQLite: профиль, состояния здоровья, приёмы пищи."""
import json
import sqlite3
from datetime import datetime, date
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


def meals_for_day(day: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM meals WHERE day=? ORDER BY ts", (day,)
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
