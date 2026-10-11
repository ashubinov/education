"""Планировщик: чистая логика без базы — проверка и очистка данных, расписание повторов.

Все даты — строки «ГГГГ-ММ-ДД» (локальный день пользователя), время — «ЧЧ:ММ». Дни недели как в JavaScript: 0 — воскресенье … 6 — суббота
(так же считает и интерфейс, поэтому правило повтора «по дням недели» одинаково понимают и сервер, и браузер).
"""
import re
from datetime import date, timedelta

MAX_TITLE = 300
MAX_NOTE = 4000
MAX_LABELS = 10
MAX_LABEL_LEN = 40
MAX_DEPTH = 3          # задача → подзадача → подподзадача
MAX_TASKS = 3000       # на одного пользователя
MAX_PROJECTS = 100
MAX_PROJECT_NAME = 80
RECUR_KINDS = ("day", "weekdays", "week", "month")

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def parse_day(s) -> date | None:
    """«2026-10-11» → date; всё остальное (в том числе 2026-02-30) → None."""
    if not isinstance(s, str) or not _DAY_RE.match(s):
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def clean_due(s) -> str | None:
    return s if parse_day(s) else None


def clean_time(s) -> str | None:
    return s if isinstance(s, str) and _TIME_RE.match(s) else None


def clean_title(s) -> str:
    return re.sub(r"\s+", " ", s or "").strip()[:MAX_TITLE]


def clean_note(s) -> str:
    return (s or "")[:MAX_NOTE]


def clean_priority(p) -> int:
    return p if isinstance(p, int) and not isinstance(p, bool) and 1 <= p <= 4 else 4


def clean_labels(items) -> list[str]:
    out: list[str] = []
    for raw in items or []:
        if not isinstance(raw, str):
            continue
        v = re.sub(r"\s+", " ", raw.strip().lstrip("@")).strip().lower()[:MAX_LABEL_LEN]
        if v and v not in out and len(out) < MAX_LABELS:
            out.append(v)
    return out


def clean_project_name(s) -> str:
    return re.sub(r"\s+", " ", s or "").strip()[:MAX_PROJECT_NAME]


def clean_emoji(s, default="📁") -> str:
    v = (s or "").strip()[:8]
    return v or default


# ----------------------------------------------------------------------------- повторы
def js_weekday(d: date) -> int:
    """0 — воскресенье … 6 — суббота."""
    return (d.weekday() + 1) % 7


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _days_in_month(y: int, m: int) -> int:
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return (nxt - date(y, m, 1)).days


def clean_recur(r) -> dict | None:
    """Правило повтора {kind, every, days?} или None, если оно неверное. Возвращает очищенную копию."""
    if not isinstance(r, dict) or r.get("kind") not in RECUR_KINDS:
        return None
    every = r.get("every", 1)
    if not isinstance(every, int) or isinstance(every, bool) or not 1 <= every <= 365:
        return None
    out = {"kind": r["kind"], "every": every}
    days = r.get("days")
    if days is not None:
        if not isinstance(days, list) or any(not isinstance(d, int) or isinstance(d, bool) or not 0 <= d <= 6 for d in days):
            return None
        if days and r["kind"] == "week":
            out["days"] = sorted(set(days))
    return out


def occurs_on(rule: dict, anchor: str, day: str) -> bool:
    """Попадает ли день `day` в расписание правила, если опорный день (первый срок) — `anchor`."""
    a, d = parse_day(anchor), parse_day(day)
    if not a or not d or d < a:
        return False
    every = max(1, min(365, int(rule.get("every") or 1)))
    kind = rule["kind"]
    if kind == "day":
        return (d - a).days % every == 0
    if kind == "weekdays":
        return 1 <= js_weekday(d) <= 5
    if kind == "week":
        days = rule.get("days") or [js_weekday(a)]
        if js_weekday(d) not in days:
            return False
        return ((monday_of(d) - monday_of(a)).days // 7) % every == 0
    if kind == "month":
        gap = (d.year - a.year) * 12 + (d.month - a.month)
        if gap < 0 or gap % every:
            return False
        return d.day == min(a.day, _days_in_month(d.year, d.month))
    return False


def next_due(rule: dict, due: str | None, today: str) -> str:
    """Следующий срок повторяющейся задачи: первый подходящий день строго позже сегодняшнего и позже текущего срока.
    Просроченная ежедневная задача после выполнения переезжает на завтра, а не копит хвост."""
    t = parse_day(today)
    anchor = parse_day(due) or t
    cutoff = max(anchor, t)
    for i in range(1, 3701):
        day = cutoff + timedelta(days=i)
        if occurs_on(rule, anchor.isoformat(), day.isoformat()):
            return day.isoformat()
    return (cutoff + timedelta(days=1)).isoformat()


def first_due(rule: dict, today: str) -> str:
    """Первый срок у новой повторяющейся задачи без явной даты: сегодня или ближайший подходящий день (сегодня включительно)."""
    t = parse_day(today)
    if rule["kind"] == "weekdays" or (rule["kind"] == "week" and rule.get("days")):
        for i in range(14):
            d = t + timedelta(days=i)
            w = js_weekday(d)
            if (1 <= w <= 5) if rule["kind"] == "weekdays" else w in rule["days"]:
                return d.isoformat()
    return today
