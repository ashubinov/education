"""Опыт, уровни, серии, цели, статистика, достижения."""
import math
from datetime import date, datetime, timedelta

from .db import db

ACHIEVEMENTS = [
    ("first_lesson", "🌱", "Первый шаг", "Пройден первый урок"),
    ("lessons_10", "📚", "Втянулся", "Пройдено 10 уроков"),
    ("lessons_50", "🏛️", "Книжный червь", "Пройдено 50 уроков"),
    ("lessons_150", "🧙", "Мудрец", "Пройдено 150 уроков"),
    ("streak_3", "🔥", "Разогрев", "3 дня подряд"),
    ("streak_7", "☄️", "Неделя огня", "7 дней подряд"),
    ("streak_30", "🌋", "Несгораемый", "30 дней подряд"),
    ("perfect_test", "💯", "Без единой ошибки", "Тест на 100%"),
    ("terms_20", "🧠", "Словарный запас", "20 терминов написано верно"),
    ("practice_1", "🛠️", "Руки в деле", "Выполнено практическое задание"),
    ("practice_5", "⚙️", "Практик", "Выполнено 5 практических заданий"),
    ("module_1", "🧩", "Модуль закрыт", "Пройден первый модуль"),
    ("course_done", "🏆", "Курс покорён", "Курс пройден до конца"),
    ("xp_100", "⭐", "Сотня", "100 очков опыта"),
    ("xp_1000", "🌟", "Тысячник", "1000 очков опыта"),
    ("level_5", "🚀", "Пятый уровень", "Достигнут 5 уровень"),
    ("level_10", "👑", "Десятый уровень", "Достигнут 10 уровень"),
    ("goal_set", "🎯", "Цель поставлена", "Поставлена цель на курс"),
    ("tg_linked", "✈️", "На связи", "Подключён Telegram-бот"),
    ("night_owl", "🦉", "Сова", "Урок после 23:00"),
    ("early_bird", "🐦", "Жаворонок", "Урок до 7:00"),
    ("comeback", "💪", "Не сдался", "Контрольный тест сдан после работы над ошибками"),
    ("sosun", "🍼", "Сосун", "Честно ответил «ДА» на главный вопрос"),
]
HIDDEN = {"sosun"}  # скрытые достижения: в списке видны, только когда получены
ACH_BY_KEY = {a[0]: a for a in ACHIEVEMENTS}


def today() -> str:
    return date.today().isoformat()


def needed_xp(level: int) -> int:
    return 100 * level * (level - 1)


def level_info(xp: int) -> dict:
    lvl = max(1, int((1 + math.sqrt(1 + xp / 25)) / 2))
    while needed_xp(lvl + 1) <= xp:
        lvl += 1
    while lvl > 1 and needed_xp(lvl) > xp:
        lvl -= 1
    lo, hi = needed_xp(lvl), needed_xp(lvl + 1)
    return {"level": lvl, "xp": xp, "into": xp - lo, "span": hi - lo, "pct": round((xp - lo) / (hi - lo) * 100)}


def total_xp(user_id: int, course_id: int | None = None) -> int:
    if course_id is None:
        return db.val("SELECT SUM(xp) FROM activity WHERE user_id=?", (user_id,), 0)
    return db.val("SELECT SUM(xp) FROM activity WHERE user_id=? AND course_id=?", (user_id, course_id), 0)


def record_activity(user_id: int, course_id: int, *, xp=0, seconds=0, lessons=0, answers=0, correct=0.0):
    d = today()
    db.x("""INSERT INTO activity(user_id,course_id,day,xp,lessons,seconds,answers,correct) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id,course_id,day) DO UPDATE SET xp=xp+excluded.xp, lessons=lessons+excluded.lessons,
            seconds=seconds+excluded.seconds, answers=answers+excluded.answers, correct=correct+excluded.correct""",
         (user_id, course_id, d, int(xp), lessons, int(seconds), answers, correct))


def _active_days(user_id: int, course_id: int | None = None) -> set[str]:
    if course_id is None:
        rows = db.q("SELECT DISTINCT day FROM activity WHERE user_id=? AND lessons>0", (user_id,))
    else:
        rows = db.q("SELECT DISTINCT day FROM activity WHERE user_id=? AND course_id=? AND lessons>0", (user_id, course_id))
    return {r["day"] for r in rows}


def streak_info(user_id: int, course_id: int | None = None) -> dict:
    days = _active_days(user_id, course_id)
    t = date.today()
    cur = 0
    d = t if t.isoformat() in days else t - timedelta(days=1)
    while d.isoformat() in days:
        cur += 1
        d -= timedelta(days=1)
    best = run = 0
    prev = None
    for s in sorted(days):
        dd = date.fromisoformat(s)
        run = run + 1 if prev and (dd - prev).days == 1 else 1
        best = max(best, run)
        prev = dd
    return {"current": cur, "best": best, "done_today": t.isoformat() in days,
            "at_risk": cur > 0 and t.isoformat() not in days, "days_total": len(days)}


def heatmap(user_id: int, course_id: int | None = None, weeks: int = 52) -> dict:
    end = date.today()
    # колонки — недели (пн–вс)
    start = end - timedelta(days=(weeks - 1) * 7 + end.weekday())  # понедельник первой колонки
    args = [user_id, start.isoformat()]
    sql = "SELECT day, SUM(xp) xp, SUM(lessons) lessons, SUM(seconds) seconds, SUM(answers) answers FROM activity WHERE user_id=? AND day>=?"
    if course_id:
        sql += " AND course_id=?"
        args.append(course_id)
    sql += " GROUP BY day"
    data = {r["day"]: r for r in db.q(sql, tuple(args))}
    days = []
    d = start
    while d <= end:
        r = data.get(d.isoformat())
        xp = (r or {}).get("xp") or 0
        level = 0 if not r or (not xp and not r["lessons"]) else 1 if xp < 30 else 2 if xp < 80 else 3 if xp < 150 else 4
        days.append({"day": d.isoformat(), "wd": d.weekday(), "xp": xp, "lessons": (r or {}).get("lessons") or 0,
                     "minutes": round(((r or {}).get("seconds") or 0) / 60), "level": level})
        d += timedelta(days=1)
    return {"days": days, "start": start.isoformat()}


def week(user_id: int, course_id: int | None = None) -> list[dict]:
    end = date.today()
    start = end - timedelta(days=6)
    args = [user_id, start.isoformat()]
    sql = "SELECT day, SUM(xp) xp, SUM(lessons) lessons, SUM(seconds) seconds FROM activity WHERE user_id=? AND day>=?"
    if course_id:
        sql += " AND course_id=?"
        args.append(course_id)
    sql += " GROUP BY day"
    data = {r["day"]: r for r in db.q(sql, tuple(args))}
    names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    out = []
    for i in range(7):
        d = start + timedelta(days=i)
        r = data.get(d.isoformat()) or {}
        out.append({"day": d.isoformat(), "label": names[d.weekday()], "xp": r.get("xp") or 0,
                    "lessons": r.get("lessons") or 0, "minutes": round((r.get("seconds") or 0) / 60),
                    "today": d == end})
    return out


# ------------------------------------------------------------------ цели
DEFAULT_MODULE_MINUTES = 28


def course_minutes_left(course_id: int) -> tuple[int, int, int]:
    """(осталось минут, осталось уроков, всего уроков) с оценкой по ещё не спланированным модулям."""
    mods = db.q("SELECT id, status, kind FROM modules WHERE course_id=? ORDER BY idx", (course_id,))
    mins_left = lessons_left = lessons_total = 0
    planned_counts, planned_mins = [], []
    per = {r["module_id"]: r for r in db.q(
        """SELECT module_id, COUNT(*) n, SUM(CASE WHEN status='done' THEN 0 ELSE 1 END) nleft,
                  SUM(CASE WHEN status='done' THEN 0 ELSE minutes END) mleft, SUM(minutes) mtot
           FROM lessons WHERE course_id=? GROUP BY module_id""", (course_id,))}
    for m in mods:
        r = per.get(m["id"])
        if r:
            planned_counts.append(r["n"])
            planned_mins.append(r["mtot"] or 0)
    avg_n = round(sum(planned_counts) / len(planned_counts)) if planned_counts else 8
    avg_m = round(sum(planned_mins) / len(planned_mins)) if planned_mins else DEFAULT_MODULE_MINUTES
    for m in mods:
        r = per.get(m["id"])
        if r:
            lessons_total += r["n"]
            lessons_left += r["nleft"] or 0
            mins_left += r["mleft"] or 0
        elif m["status"] != "done":
            lessons_total += avg_n
            lessons_left += avg_n
            mins_left += avg_m
    return int(mins_left), int(lessons_left), int(lessons_total)


def goal_info(course: dict, user_id: int) -> dict | None:
    gd = course.get("goal_date")
    if not gd:
        return None
    mins_left, lessons_left, _ = course_minutes_left(course["id"])
    try:
        target = date.fromisoformat(gd)
    except ValueError:
        return None
    days_left = (target - date.today()).days + 1
    today_s = db.val("SELECT seconds FROM activity WHERE user_id=? AND course_id=? AND day=?",
                     (user_id, course["id"], today()), 0) or 0
    spent_today = round(today_s / 60, 1)
    # норма на день — от остатка на начало сегодняшнего дня
    mins_left_start = mins_left + spent_today
    if days_left <= 0:
        per_day = mins_left
        status = "overdue" if mins_left > 0 else "done"
    else:
        per_day = mins_left_start / days_left
        status = "done" if mins_left == 0 else "ok"
    per_day_i = max(1, math.ceil(per_day)) if mins_left > 0 else 0
    return {"date": gd, "days_left": days_left, "minutes_left": mins_left, "lessons_left": lessons_left,
            "per_day_minutes": per_day_i, "spent_today": spent_today,
            "lessons_per_day": round(lessons_left / max(days_left, 1), 1) if days_left > 0 else lessons_left,
            "status": status, "reached_today": spent_today >= per_day_i and per_day_i > 0,
            "hard": per_day_i > 90}


# ------------------------------------------------------------------ достижения
def _stats_for_ach(user_id: int) -> dict:
    xp = total_xp(user_id)
    lessons = db.val("SELECT SUM(lessons) FROM activity WHERE user_id=?", (user_id,), 0) or 0
    s = streak_info(user_id)
    perfect = db.val("""SELECT COUNT(*) FROM runs r JOIN lessons l ON l.id=r.lesson_id
                        WHERE r.user_id=? AND r.finished_at IS NOT NULL AND r.score>=0.999
                        AND l.type IN ('intro_test','terms_test','methods_test','final_test')""", (user_id,), 0)
    terms_ok = db.val("SELECT COUNT(*) FROM answers WHERE user_id=? AND kind='write' AND correct>=0.99", (user_id,), 0)
    practice_ok = db.val("""SELECT COUNT(*) FROM runs r JOIN lessons l ON l.id=r.lesson_id
                            WHERE r.user_id=? AND l.type='practice' AND r.finished_at IS NOT NULL AND r.score>=0.5""", (user_id,), 0)
    modules_done = db.val("""SELECT COUNT(*) FROM modules m JOIN courses c ON c.id=m.course_id
                             WHERE c.user_id=? AND m.status='done'""", (user_id,), 0)
    courses_done = db.val("SELECT COUNT(*) FROM courses WHERE user_id=? AND status='completed'", (user_id,), 0)
    has_goal = db.val("SELECT COUNT(*) FROM courses WHERE user_id=? AND goal_date IS NOT NULL", (user_id,), 0)
    u = db.one("SELECT tg_chat_id FROM users WHERE id=?", (user_id,)) or {}
    h = datetime.now().hour
    return {"xp": xp, "lessons": lessons, "streak": max(s["current"], s["best"]), "perfect": perfect,
            "terms_ok": terms_ok, "practice_ok": practice_ok, "modules_done": modules_done,
            "courses_done": courses_done, "has_goal": has_goal, "tg": bool(u.get("tg_chat_id")),
            "level": level_info(xp)["level"], "hour": h}


def _earned(st: dict, extra: set[str]) -> set[str]:
    e = set(extra)
    if st["lessons"] >= 1: e.add("first_lesson")
    if st["lessons"] >= 10: e.add("lessons_10")
    if st["lessons"] >= 50: e.add("lessons_50")
    if st["lessons"] >= 150: e.add("lessons_150")
    if st["streak"] >= 3: e.add("streak_3")
    if st["streak"] >= 7: e.add("streak_7")
    if st["streak"] >= 30: e.add("streak_30")
    if st["perfect"] >= 1: e.add("perfect_test")
    if st["terms_ok"] >= 20: e.add("terms_20")
    if st["practice_ok"] >= 1: e.add("practice_1")
    if st["practice_ok"] >= 5: e.add("practice_5")
    if st["modules_done"] >= 1: e.add("module_1")
    if st["courses_done"] >= 1: e.add("course_done")
    if st["xp"] >= 100: e.add("xp_100")
    if st["xp"] >= 1000: e.add("xp_1000")
    if st["level"] >= 5: e.add("level_5")
    if st["level"] >= 10: e.add("level_10")
    if st["has_goal"]: e.add("goal_set")
    if st["tg"]: e.add("tg_linked")
    return e


def check_achievements(user_id: int, extra: set[str] | None = None) -> list[dict]:
    """Выдать новые достижения. extra — события, которые не видны из статистики (night_owl и т.п.)."""
    st = _stats_for_ach(user_id)
    ex = set(extra or set())
    if st["lessons"] and st["hour"] >= 23 and "lesson_now" in ex:
        ex.add("night_owl")
    if st["lessons"] and st["hour"] < 7 and "lesson_now" in ex:
        ex.add("early_bird")
    earned = _earned(st, ex)
    have = {r["key"] for r in db.q("SELECT key FROM achievements WHERE user_id=?", (user_id,))}
    new = []
    for key in earned - have:
        if key in ACH_BY_KEY:
            db.x("INSERT OR IGNORE INTO achievements(user_id,key) VALUES(?,?)", (user_id, key))
            a = ACH_BY_KEY[key]
            new.append({"key": a[0], "icon": a[1], "title": a[2], "desc": a[3]})
    return new


def achievements_list(user_id: int) -> list[dict]:
    got = {r["key"]: r["unlocked_at"] for r in db.q("SELECT key, unlocked_at FROM achievements WHERE user_id=?", (user_id,))}
    return [{"key": a[0], "icon": a[1], "title": a[2], "desc": a[3], "unlocked": a[0] in got,
             "at": got.get(a[0])} for a in ACHIEVEMENTS if a[0] not in HIDDEN or a[0] in got]


def user_summary(user_id: int) -> dict:
    xp = total_xp(user_id)
    return {"level": level_info(xp), "streak": streak_info(user_id)}
