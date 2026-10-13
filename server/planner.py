"""Планировщик: «Сегодня», «Задачи» и «Календарь».

• Задачи (аналог Todoist): проекты, подзадачи (до трёх уровней), приоритеты 1–4, срок и время, метки, повторы
  («каждый день», «по будням», «каждые 2 недели», «по вторникам и четвергам», «каждый месяц»).
  Повторяющаяся задача при отметке не закрывается, а переезжает на следующий срок.
• «Сегодня» — один список на день: что по целям курсов (сколько минут ещё нужно), следующий урок, задачи на сегодня и просроченные.
• «Календарь» — для недели/месяца отдаёт цели курсов (дедлайны), пройденные тесты и минуты занятий; задачи фронт берёт из /api/tasks
  и сам раскладывает по дням (включая будущие повторы по тем же правилам).

Всё принадлежит пользователю: чужие задачи и проекты не видны и не меняются. Что нужно знать о датах: «сегодня» присылает браузер
(локальный день человека), сервер принимает его, только если он не дальше двух суток от серверного дня.
"""
import json
import time
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from . import auth, generator
from . import gamification as gm
from .db import db, jd, jl
from .planner_data import (MAX_DEPTH, MAX_NOTE, MAX_PROJECTS, MAX_TASKS, clean_due, clean_emoji, clean_labels, clean_note, clean_priority,
                           clean_project_name, clean_recur, clean_time, clean_title, first_due, next_due, parse_day)

router = APIRouter(prefix="/api", tags=["Планировщик"])

TEST_TYPES = ("intro_test", "terms_test", "methods_test", "final_test")
TEST_NAMES = {"intro_test": "Входной тест", "terms_test": "Тест по терминам", "methods_test": "Тест по методам", "final_test": "Итоговый тест"}
MAX_RANGE_DAYS = 70


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskCreate(Strict):
    title: str = Field(min_length=1, max_length=3000)
    note: str = Field(default="", max_length=20000)
    project_id: int | None = Field(default=None, ge=1)
    parent_id: int | None = Field(default=None, ge=1)
    priority: int = Field(default=4, ge=1, le=4)
    due: str | None = None
    due_time: str | None = None
    recur: dict | None = None
    labels: list[str] = Field(default_factory=list, max_length=100)
    today: str | None = Field(default=None, max_length=10)


class TaskPatch(Strict):
    title: str | None = Field(default=None, max_length=3000)
    note: str | None = Field(default=None, max_length=20000)
    project_id: int | None = Field(default=None, ge=1)
    parent_id: int | None = Field(default=None, ge=1)
    priority: int | None = Field(default=None, ge=1, le=4)
    due: str | None = None
    due_time: str | None = None
    recur: dict | None = None
    labels: list[str] | None = Field(default=None, max_length=100)
    today: str | None = Field(default=None, max_length=10)


class TodayIn(Strict):
    today: str | None = Field(default=None, max_length=10)


class RestoreIn(Strict):
    tasks: list[dict] = Field(max_length=500)


class ProjectIn(Strict):
    name: str = Field(min_length=1, max_length=400)
    emoji: str = Field(default="📁", max_length=40)


class ProjectPatch(Strict):
    name: str | None = Field(default=None, max_length=400)
    emoji: str | None = Field(default=None, max_length=40)


def init_schema():
    with db.lock:
        db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS planner_projects(
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL, emoji TEXT NOT NULL DEFAULT '📁',
          ord INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS planner_tasks(
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
          project_id INTEGER, parent_id INTEGER, priority INTEGER NOT NULL DEFAULT 4, due TEXT, due_time TEXT, recur TEXT,
          labels TEXT NOT NULL DEFAULT '[]', done INTEGER NOT NULL DEFAULT 0, done_at REAL,
          created_at REAL NOT NULL, updated_at REAL NOT NULL, ord REAL NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS ix_planner_tasks_user ON planner_tasks(user_id, done, due);
        CREATE INDEX IF NOT EXISTS ix_planner_tasks_parent ON planner_tasks(user_id, parent_id);
        CREATE INDEX IF NOT EXISTS ix_planner_projects_user ON planner_projects(user_id, ord);
        """)
        db.conn.commit()


# ----------------------------------------------------------------------------- представление
def _view(r: dict) -> dict:
    return {"id": r["id"], "title": r["title"], "note": r["note"], "project_id": r["project_id"], "parent_id": r["parent_id"],
            "priority": r["priority"], "due": r["due"], "due_time": r["due_time"], "recur": jl(r["recur"], None), "labels": jl(r["labels"], []),
            "done": bool(r["done"]), "done_at": r["done_at"], "created_at": r["created_at"], "updated_at": r["updated_at"], "order": r["ord"]}


def _project_view(r: dict) -> dict:
    return {"id": r["id"], "name": r["name"], "emoji": r["emoji"], "order": r["ord"], "created_at": r["created_at"]}


def _all_rows(uid: int) -> list[dict]:
    return db.q("SELECT * FROM planner_tasks WHERE user_id=? ORDER BY ord, id", (uid,))


def _one(uid: int, tid: int) -> dict:
    r = db.one("SELECT * FROM planner_tasks WHERE id=? AND user_id=?", (tid, uid))
    if not r:
        raise HTTPException(404, "Задача не найдена")
    return r


def _descendants(rows: list[dict], tid: int) -> list[dict]:
    out, queue, seen = [], [tid], {tid}
    while queue:
        cur = queue.pop(0)
        for r in rows:
            if r["parent_id"] == cur and r["id"] not in seen:
                seen.add(r["id"])
                out.append(r)
                queue.append(r["id"])
    return out


def _depth(by_id: dict, tid: int | None) -> int:
    d, cur, seen = 0, tid, set()
    while cur and cur not in seen and d < 50:
        seen.add(cur)
        d += 1
        cur = (by_id.get(cur) or {}).get("parent_id")
    return d


def _height(rows: list[dict], tid: int, seen: frozenset = frozenset()) -> int:
    kids = [r for r in rows if r["parent_id"] == tid and r["id"] not in seen]
    return 1 + max(_height(rows, k["id"], seen | {tid}) for k in kids) if kids else 0


def _today(s) -> str:
    """День пользователя: принимаем присланный браузером, если он правдоподобен (часовые пояса), иначе серверный."""
    d = parse_day(s)
    srv = date.today()
    if d and abs((d - srv).days) <= 2:
        return d.isoformat()
    return gm.today()


def _check_due(v):
    if v is not None and not clean_due(v):
        raise HTTPException(400, "Неверная дата")
    return v


def _check_time(v):
    if v is not None and not clean_time(v):
        raise HTTPException(400, "Неверное время (нужно ЧЧ:ММ)")
    return v


def _check_recur(v):
    if v is None:
        return None
    r = clean_recur(v)
    if not r:
        raise HTTPException(400, "Неверное правило повтора")
    return r


def _own_project(uid: int, pid: int | None) -> int | None:
    if pid is None:
        return None
    if not db.val("SELECT 1 FROM planner_projects WHERE id=? AND user_id=?", (pid, uid)):
        raise HTTPException(404, "Проект не найден")
    return pid


# ----------------------------------------------------------------------------- задачи
def get_state(uid: int) -> dict:
    return {"tasks": [_view(r) for r in _all_rows(uid)], "projects": [_project_view(r) for r in db.q("SELECT * FROM planner_projects WHERE user_id=? ORDER BY ord, id", (uid,))],
            "limits": {"title": 300, "note": MAX_NOTE, "depth": MAX_DEPTH, "tasks": MAX_TASKS, "projects": MAX_PROJECTS}}


def create_task(uid: int, b: TaskCreate) -> dict:
    title = clean_title(b.title)
    if not title:
        raise HTTPException(400, "Название не может быть пустым")
    today = _today(b.today)
    recur = _check_recur(b.recur)
    due, due_time = _check_due(b.due), _check_time(b.due_time)
    now = time.time()
    with db.tx():
        rows = _all_rows(uid)
        if len(rows) >= MAX_TASKS:
            raise HTTPException(400, f"Не больше {MAX_TASKS} задач: удали выполненные")
        by_id = {r["id"]: r for r in rows}
        project_id = _own_project(uid, b.project_id)
        parent_id = None
        if b.parent_id:
            parent = by_id.get(b.parent_id)
            if not parent:
                raise HTTPException(404, "Родительская задача не найдена")
            if _depth(by_id, parent["id"]) >= MAX_DEPTH:
                raise HTTPException(400, f"Вложенность — не больше {MAX_DEPTH} уровней")
            parent_id, project_id, recur = parent["id"], parent["project_id"], None  # подзадача живёт в проекте родителя и не повторяется
        if recur and not due:
            due = first_due(recur, today)
        if not due:
            due_time = None
        tid = db.x("INSERT INTO planner_tasks(user_id,title,note,project_id,parent_id,priority,due,due_time,recur,labels,done,done_at,created_at,updated_at,ord) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,0,NULL,?,?,?)",
                   (uid, title, clean_note(b.note), project_id, parent_id, clean_priority(b.priority), due, due_time, jd(recur) if recur else None,
                    jd(clean_labels(b.labels)), now, now, now * 1000))
    return _view(db.one("SELECT * FROM planner_tasks WHERE id=?", (tid,)))


def update_task(uid: int, tid: int, b: TaskPatch) -> dict:
    sent = b.model_fields_set - {"today"}
    today = _today(b.today)
    now = time.time()
    with db.tx():
        rows = _all_rows(uid)
        by_id = {r["id"]: r for r in rows}
        cur = by_id.get(tid)
        if not cur:
            raise HTTPException(404, "Задача не найдена")
        nxt = dict(cur)
        if "title" in sent:
            t = clean_title(b.title)
            if not t:
                raise HTTPException(400, "Название не может быть пустым")
            nxt["title"] = t
        if "note" in sent:
            nxt["note"] = clean_note(b.note)
        if "priority" in sent:
            if b.priority is None:
                raise HTTPException(400, "Неверный приоритет")
            nxt["priority"] = b.priority
        if "labels" in sent:
            nxt["labels"] = jd(clean_labels(b.labels))
        if "due" in sent:
            nxt["due"] = _check_due(b.due)
        if "due_time" in sent:
            nxt["due_time"] = _check_time(b.due_time)
        if "recur" in sent:
            r = _check_recur(b.recur)
            nxt["recur"] = jd(r) if r else None

        move_to = "keep"
        if "parent_id" in sent:
            if b.parent_id is None:
                nxt["parent_id"] = None
            else:
                parent = by_id.get(b.parent_id)
                kids = {d["id"] for d in _descendants(rows, tid)}
                if not parent:
                    raise HTTPException(404, "Родительская задача не найдена")
                if parent["id"] == tid or parent["id"] in kids:
                    raise HTTPException(400, "Задачу нельзя вложить в саму себя или в свою подзадачу")
                if _depth(by_id, parent["id"]) + 1 + _height(rows, tid) > MAX_DEPTH:
                    raise HTTPException(400, f"Вложенность — не больше {MAX_DEPTH} уровней")
                nxt["parent_id"] = parent["id"]
                move_to = parent["project_id"]
        if "project_id" in sent and move_to == "keep" and not nxt["parent_id"]:
            move_to = _own_project(uid, b.project_id)
        if move_to != "keep":
            nxt["project_id"] = move_to

        recur = jl(nxt["recur"], None)
        if nxt["parent_id"]:
            nxt["recur"], recur = None, None
        if recur and not nxt["due"]:
            nxt["due"] = first_due(recur, today)
        if not nxt["due"]:
            nxt["due_time"] = None
        nxt["updated_at"] = now
        db.x("UPDATE planner_tasks SET title=?,note=?,project_id=?,parent_id=?,priority=?,due=?,due_time=?,recur=?,labels=?,updated_at=? WHERE id=? AND user_id=?",
             (nxt["title"], nxt["note"], nxt["project_id"], nxt["parent_id"], nxt["priority"], nxt["due"], nxt["due_time"], nxt["recur"], nxt["labels"], now, tid, uid))
        if nxt["project_id"] != cur["project_id"]:
            for k in _descendants(rows, tid):
                db.x("UPDATE planner_tasks SET project_id=?, updated_at=? WHERE id=? AND user_id=?", (nxt["project_id"], now, k["id"], uid))
    return _view(_one(uid, tid))


def toggle_task(uid: int, tid: int, today_raw) -> dict:
    """Сделана ↔ не сделана. Повторяющаяся задача переезжает на следующий срок (подзадачи открываются заново).
    Закрытие закрывает открытые подзадачи; возврат подзадачи в работу открывает закрытых родителей."""
    today, now = _today(today_raw), time.time()
    changed: list[int] = []
    rescheduled = None
    with db.tx():
        rows = _all_rows(uid)
        by_id = {r["id"]: r for r in rows}
        cur = by_id.get(tid)
        if not cur:
            raise HTTPException(404, "Задача не найдена")
        if cur["done"]:
            db.x("UPDATE planner_tasks SET done=0, done_at=NULL, updated_at=? WHERE id=?", (now, tid))
            changed.append(tid)
            p, seen = by_id.get(cur["parent_id"]), set()
            while p and p["done"] and p["id"] not in seen:
                seen.add(p["id"])
                db.x("UPDATE planner_tasks SET done=0, done_at=NULL, updated_at=? WHERE id=?", (now, p["id"]))
                changed.append(p["id"])
                p = by_id.get(p["parent_id"])
        else:
            kids = _descendants(rows, tid)
            recur = jl(cur["recur"], None)
            if recur:
                rescheduled = next_due(recur, cur["due"], today)
                db.x("UPDATE planner_tasks SET due=?, updated_at=? WHERE id=?", (rescheduled, now, tid))
                changed.append(tid)
                for k in kids:
                    if k["done"]:
                        db.x("UPDATE planner_tasks SET done=0, done_at=NULL, updated_at=? WHERE id=?", (now, k["id"]))
                        changed.append(k["id"])
            else:
                db.x("UPDATE planner_tasks SET done=1, done_at=?, updated_at=? WHERE id=?", (now, now, tid))
                changed.append(tid)
                for k in kids:
                    if not k["done"]:
                        db.x("UPDATE planner_tasks SET done=1, done_at=?, updated_at=? WHERE id=?", (now, now, k["id"]))
                        changed.append(k["id"])
    ph = ",".join("?" * len(changed))
    return {"task": _view(_one(uid, tid)), "changed": [_view(r) for r in db.q(f"SELECT * FROM planner_tasks WHERE user_id=? AND id IN ({ph})", (uid, *changed))],
            "rescheduled_to": rescheduled}


def delete_task(uid: int, tid: int) -> dict:
    """Удалить задачу вместе с подзадачами. Возвращает удалённые записи (для кнопки «Отменить»)."""
    with db.tx():
        rows = _all_rows(uid)
        if not any(r["id"] == tid for r in rows):
            raise HTTPException(404, "Задача не найдена")
        gone = [next(r for r in rows if r["id"] == tid)] + _descendants(rows, tid)
        ids = [r["id"] for r in gone]
        db.x(f"DELETE FROM planner_tasks WHERE user_id=? AND id IN ({','.join('?' * len(ids))})", (uid, *ids))
    return {"deleted": [_view(r) for r in gone]}


def clear_completed(uid: int) -> dict:
    """Убрать все выполненные. Невыполненная подзадача выполненного родителя становится обычной задачей."""
    now = time.time()
    with db.tx():
        rows = _all_rows(uid)
        done_ids = {r["id"] for r in rows if r["done"]}
        for r in rows:
            if not r["done"] and r["parent_id"] in done_ids:
                db.x("UPDATE planner_tasks SET parent_id=NULL, updated_at=? WHERE id=?", (now, r["id"]))
        if done_ids:
            db.x(f"DELETE FROM planner_tasks WHERE user_id=? AND id IN ({','.join('?' * len(done_ids))})", (uid, *done_ids))
    return {"deleted": len(done_ids)}


def restore_tasks(uid: int, rows_in: list[dict]) -> dict:
    """«Отменить»: вернуть задачи в прежнем виде (изменённые — обновить, удалённые — создать заново с теми же номерами)."""
    now = time.time()
    with db.tx():
        existing = {r["id"]: r for r in _all_rows(uid)}
        proj = {r["id"] for r in db.q("SELECT id FROM planner_projects WHERE user_id=?", (uid,))}
        batch = {r.get("id") for r in rows_in if isinstance(r, dict) and isinstance(r.get("id"), int)}
        if len(existing) + len([i for i in batch if i not in existing]) > MAX_TASKS:
            raise HTTPException(400, f"Не больше {MAX_TASKS} задач")
        touched = []
        for r in rows_in:
            tid = r.get("id")
            if not isinstance(tid, int) or isinstance(tid, bool) or tid < 1:
                continue
            title = clean_title(r.get("title"))
            if not title:
                continue
            if tid not in existing and db.val("SELECT 1 FROM planner_tasks WHERE id=?", (tid,)):
                continue  # номер занят чужой задачей
            recur = clean_recur(r.get("recur")) if r.get("recur") else None
            due = clean_due(r.get("due"))
            vals = (title, clean_note(r.get("note")), r.get("project_id") if r.get("project_id") in proj else None,
                    r.get("parent_id") if r.get("parent_id") in (set(existing) | batch) and r.get("parent_id") != tid else None, clean_priority(r.get("priority")),
                    due, clean_time(r.get("due_time")) if due else None, jd(recur) if recur else None, jd(clean_labels(r.get("labels"))),
                    1 if r.get("done") else 0, r.get("done_at") if isinstance(r.get("done_at"), (int, float)) and r.get("done") else None)
            if tid in existing:
                db.x("UPDATE planner_tasks SET title=?,note=?,project_id=?,parent_id=?,priority=?,due=?,due_time=?,recur=?,labels=?,done=?,done_at=?,updated_at=? WHERE id=? AND user_id=?",
                     (*vals, now, tid, uid))
            else:
                created = r.get("created_at") if isinstance(r.get("created_at"), (int, float)) else now
                order = r.get("order") if isinstance(r.get("order"), (int, float)) else now * 1000
                db.x("INSERT INTO planner_tasks(id,user_id,title,note,project_id,parent_id,priority,due,due_time,recur,labels,done,done_at,created_at,updated_at,ord) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (tid, uid, *vals, created, now, order))
            touched.append(tid)
        _fix_hierarchy(uid)
    ph = ",".join("?" * len(touched)) or "NULL"
    return {"tasks": [_view(r) for r in db.q(f"SELECT * FROM planner_tasks WHERE user_id=? AND id IN ({ph})", (uid, *touched))]}


def _fix_hierarchy(uid: int):
    """После восстановления: разорвать циклы и слишком глубокие цепочки, подзадачи берут проект родителя."""
    rows = _all_rows(uid)
    by_id = {r["id"]: r for r in rows}
    for r in rows:
        if not r["parent_id"]:
            continue
        chain, cur, bad = [], r["id"], False
        while cur:
            if cur in chain:
                bad = True
                break
            chain.append(cur)
            cur = (by_id.get(cur) or {}).get("parent_id")
        if bad or len(chain) > MAX_DEPTH or r["parent_id"] not in by_id:
            db.x("UPDATE planner_tasks SET parent_id=NULL WHERE id=?", (r["id"],))
            r["parent_id"] = None
    for r in rows:
        if r["parent_id"]:
            p = by_id[r["parent_id"]]
            if p["project_id"] != r["project_id"] or r["recur"]:
                db.x("UPDATE planner_tasks SET project_id=?, recur=NULL WHERE id=?", (p["project_id"], r["id"]))


# ----------------------------------------------------------------------------- проекты
def add_project(uid: int, name: str, emoji: str) -> dict:
    n = clean_project_name(name)
    if not n:
        raise HTTPException(400, "Название проекта не может быть пустым")
    with db.tx():
        rows = db.q("SELECT * FROM planner_projects WHERE user_id=?", (uid,))
        dup = next((r for r in rows if r["name"].lower() == n.lower()), None)
        if dup:
            return _project_view(dup)
        if len(rows) >= MAX_PROJECTS:
            raise HTTPException(400, f"Не больше {MAX_PROJECTS} проектов")
        pid = db.x("INSERT INTO planner_projects(user_id,name,emoji,ord,created_at) VALUES(?,?,?,?,?)",
                   (uid, n, clean_emoji(emoji), max((r["ord"] for r in rows), default=-1) + 1, time.time()))
    return _project_view(db.one("SELECT * FROM planner_projects WHERE id=?", (pid,)))


def update_project(uid: int, pid: int, b: ProjectPatch) -> dict:
    with db.tx():
        cur = db.one("SELECT * FROM planner_projects WHERE id=? AND user_id=?", (pid, uid))
        if not cur:
            raise HTTPException(404, "Проект не найден")
        name, emoji = cur["name"], cur["emoji"]
        if b.name is not None:
            n = clean_project_name(b.name)
            if not n:
                raise HTTPException(400, "Название проекта не может быть пустым")
            # сравнение в Python: SQLite lower() не понимает кириллицу
            if any(r["name"].lower() == n.lower() for r in db.q("SELECT name FROM planner_projects WHERE user_id=? AND id<>?", (uid, pid))):
                raise HTTPException(400, "Проект с таким названием уже есть")
            name = n
        if b.emoji is not None:
            emoji = clean_emoji(b.emoji, cur["emoji"])
        db.x("UPDATE planner_projects SET name=?, emoji=? WHERE id=?", (name, emoji, pid))
    return _project_view(db.one("SELECT * FROM planner_projects WHERE id=?", (pid,)))


def delete_project(uid: int, pid: int) -> dict:
    """Удалить проект вместе со всеми его задачами."""
    with db.tx():
        if not db.val("SELECT 1 FROM planner_projects WHERE id=? AND user_id=?", (pid, uid)):
            raise HTTPException(404, "Проект не найден")
        n = db.val("SELECT COUNT(*) FROM planner_tasks WHERE user_id=? AND project_id=?", (uid, pid), 0)
        db.x("DELETE FROM planner_tasks WHERE user_id=? AND project_id=?", (uid, pid))
        db.x("DELETE FROM planner_projects WHERE id=?", (pid,))
    return {"deleted_tasks": n}


def delete_user_data(uid: int):
    db.x("DELETE FROM planner_tasks WHERE user_id=?", (uid,))
    db.x("DELETE FROM planner_projects WHERE user_id=?", (uid,))


# ----------------------------------------------------------------------------- «Сегодня»
def today_plan(uid: int, today_raw=None) -> dict:
    """Список дел на день. Обязательные пункты считаются в проценты «сегодня»:
    цели курсов (минуты, которые ещё нужны сегодня), просроченные цели, задачи (просрочено/на сегодня) и — если иначе делать нечего — следующий урок."""
    today = _today(today_raw)
    items: list[dict] = []
    courses = db.q("SELECT * FROM courses WHERE user_id=? AND is_template=0 AND status='ready' ORDER BY COALESCE(last_opened, created_at) DESC", (uid,))
    lessons_today = db.val("SELECT SUM(lessons) FROM activity WHERE user_id=? AND day=?", (uid, gm.today()), 0) or 0
    first_free = None
    for c in courses:
        g = gm.goal_info(c, uid)
        nl = generator.next_lesson(c["id"])
        base = {"course_id": c["id"], "icon": c["icon"], "title": c["title"], "to": f"#/learn/{c['id']}", "next_title": nl["title"] if nl else None}
        if g and g["status"] == "ok":
            left = max(0, round(g["per_day_minutes"] - g["spent_today"]))
            items.append({**base, "id": f"goal:{c['id']}", "kind": "goal", "required": True, "done": bool(g["reached_today"]), "minutes": left,
                          "detail": f"до {g['date']} · осталось {g['days_left']} дн.", "goal": g})
        elif g and g["status"] == "overdue":
            items.append({**base, "id": f"overdue:{c['id']}", "kind": "overdue", "required": True, "done": False, "minutes": min(g["minutes_left"], 45),
                          "detail": f"срок прошёл, осталось ≈{g['minutes_left']} мин", "goal": g})
        elif not g and nl and first_free is None:
            first_free = {**base, "id": f"lesson:{c['id']}", "kind": "lesson", "required": False, "done": False, "minutes": int(nl.get("minutes") or 5),
                          "detail": f"Следующий урок: {nl['title']}", "goal": None}
    rows = [r for r in _all_rows(uid) if not r["done"] and r["due"]]
    overdue = sum(1 for r in rows if r["due"] < today)
    due_today = sum(1 for r in rows if r["due"] == today)
    tomorrow = sum(1 for r in rows if r["due"] == (parse_day(today) + timedelta(days=1)).isoformat())
    done_today = sum(1 for r in _all_rows(uid) if r["done"] and r["done_at"] and time.strftime("%Y-%m-%d", time.localtime(r["done_at"])) == gm.today())
    if overdue or due_today:
        items.append({"id": "tasks", "kind": "tasks", "icon": "✅", "title": "Задачи", "to": "#/tasks?v=today", "required": True, "done": False, "minutes": 0,
                      "detail": f"просрочено {overdue} · на сегодня {due_today}", "overdue": overdue, "today": due_today})
    if first_free:
        # свободный курс: обязательный, только если больше ничего нет и сегодня уроков ещё не было
        first_free["required"] = not any(i["required"] for i in items) and not lessons_today
        first_free["done"] = bool(lessons_today) and not any(i["required"] for i in items)
        items.append(first_free)
    req = [i for i in items if i["required"]]
    streak = gm.streak_info(uid)
    percent = round(100 * sum(1 for i in req if i["done"]) / len(req)) if req else (100 if streak["done_today"] or lessons_today else 0)
    return {"date": today, "items": items, "required_total": len(req), "required_done": sum(1 for i in req if i["done"]), "percent": percent,
            "minutes_left": sum(i["minutes"] for i in req if not i["done"]), "tasks_done_today": done_today, "tasks_tomorrow": tomorrow,
            "streak": {"current": streak["current"], "done_today": streak["done_today"], "at_risk": streak["at_risk"]}}


# ----------------------------------------------------------------------------- «Календарь»
def calendar_range(uid: int, frm: str, to: str) -> dict:
    a, b = parse_day(frm), parse_day(to)
    if not a or not b or b < a:
        raise HTTPException(400, "Неверный период")
    if (b - a).days > MAX_RANGE_DAYS:
        raise HTTPException(400, f"Период — не больше {MAX_RANGE_DAYS} дней")
    goals = [{"course_id": c["id"], "title": c["title"], "icon": c["icon"], "date": c["goal_date"]}
             for c in db.q("SELECT id,title,icon,goal_date FROM courses WHERE user_id=? AND is_template=0 AND goal_date BETWEEN ? AND ? ORDER BY goal_date, id", (uid, frm, to))]
    ph = ",".join("?" * len(TEST_TYPES))
    tests = db.q(f"""SELECT date(r.finished_at,'localtime') AS day, r.score AS score, l.id AS lesson_id, l.title AS lesson_title, l.type AS type,
                            c.id AS course_id, c.title AS course_title, c.icon AS icon
                     FROM runs r JOIN lessons l ON l.id=r.lesson_id JOIN courses c ON c.id=l.course_id
                     WHERE r.user_id=? AND r.finished_at IS NOT NULL AND l.type IN ({ph}) AND date(r.finished_at,'localtime') BETWEEN ? AND ?
                     ORDER BY r.finished_at, r.id""", (uid, *TEST_TYPES, frm, to))
    for t in tests:
        t["type_name"] = TEST_NAMES.get(t["type"], "Тест")
        t["score"] = None if t["score"] is None else round(t["score"] * 100)
    study = [{"day": r["day"], "minutes": round((r["s"] or 0) / 60), "lessons": r["l"] or 0, "xp": r["x"] or 0}
             for r in db.q("SELECT day, SUM(seconds) s, SUM(lessons) l, SUM(xp) x FROM activity WHERE user_id=? AND day BETWEEN ? AND ? GROUP BY day HAVING SUM(seconds)>0 OR SUM(lessons)>0 ORDER BY day", (uid, frm, to))]
    return {"from": frm, "to": to, "goals": goals, "tests": tests, "study": study}


# ----------------------------------------------------------------------------- HTTP
def _me(request: Request) -> dict:
    return auth.require_user(request)


@router.get("/tasks")
async def api_state(request: Request):
    return await run_in_threadpool(get_state, _me(request)["id"])


@router.post("/tasks")
async def api_create(request: Request, body: TaskCreate):
    return await run_in_threadpool(create_task, _me(request)["id"], body)


@router.post("/tasks/restore")
async def api_restore(request: Request, body: RestoreIn):
    return await run_in_threadpool(restore_tasks, _me(request)["id"], body.tasks)


@router.delete("/tasks/completed")
async def api_clear_completed(request: Request):
    return await run_in_threadpool(clear_completed, _me(request)["id"])


@router.put("/tasks/{tid}")
async def api_update(request: Request, body: TaskPatch, tid: int = Path(ge=1, le=10**12)):
    return await run_in_threadpool(update_task, _me(request)["id"], tid, body)


@router.post("/tasks/{tid}/toggle")
async def api_toggle(request: Request, body: TodayIn, tid: int = Path(ge=1, le=10**12)):
    return await run_in_threadpool(toggle_task, _me(request)["id"], tid, body.today)


@router.delete("/tasks/{tid}")
async def api_delete(request: Request, tid: int = Path(ge=1, le=10**12)):
    return await run_in_threadpool(delete_task, _me(request)["id"], tid)


@router.post("/task-projects")
async def api_project_add(request: Request, body: ProjectIn):
    return await run_in_threadpool(add_project, _me(request)["id"], body.name, body.emoji)


@router.put("/task-projects/{pid}")
async def api_project_update(request: Request, body: ProjectPatch, pid: int = Path(ge=1, le=10**12)):
    return await run_in_threadpool(update_project, _me(request)["id"], pid, body)


@router.delete("/task-projects/{pid}")
async def api_project_delete(request: Request, pid: int = Path(ge=1, le=10**12)):
    return await run_in_threadpool(delete_project, _me(request)["id"], pid)


@router.get("/today")
async def api_today(request: Request, today: str | None = Query(default=None, max_length=10)):
    return await run_in_threadpool(today_plan, _me(request)["id"], today)


@router.get("/calendar")
async def api_calendar(request: Request, frm: str = Query(alias="from", max_length=10), to: str = Query(max_length=10)):
    return await run_in_threadpool(calendar_range, _me(request)["id"], frm, to)
