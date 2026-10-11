"""Планировщик («Сегодня», «Задачи», «Календарь»): очистка данных, повторы, создание/правка/выполнение/удаление, «Отменить», проекты,
изоляция пользователей, план на день по целям курсов, данные для календаря, удаление аккаунта.
Запуск (чистый сервер, LQ_DB — путь к его базе): LQ_DB=<tmp.db> python tests/test_planner.py http://127.0.0.1:8011"""
import os
import sqlite3
import sys
import time
from datetime import date, timedelta

import httpx

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
from server import planner_data as P  # noqa: E402

db_path = os.environ.get("LQ_DB")


def sql(q, args=()):
    con = sqlite3.connect(db_path, timeout=30)
    try:
        cur = con.execute(q, args)
        con.commit()
        return cur.fetchall()
    finally:
        con.close()


def reg(name):
    d = httpx.post(f"{BASE}/api/auth/register", json={"username": f"{name}{int(time.time()) % 100000}", "password": "secret1"}, timeout=30).json()
    return httpx.Client(base_url=BASE, timeout=60, headers={"Authorization": f"Bearer {d['token']}"}), d["user"]


def err(r, code=400):
    assert r.status_code == code, (r.status_code, r.text)
    return r.json()["detail"]


def D(n=0, base=None):
    return ((base or date.today()) + timedelta(days=n)).isoformat()


TODAY = D(0)

# ----------------------------------------------------------------------------- чистая логика
assert P.parse_day("2026-02-30") is None and P.parse_day("2026-2-3") is None and P.parse_day(5) is None and str(P.parse_day("2026-10-11")) == "2026-10-11"
assert P.clean_title("  Купить   хлеб \n") == "Купить хлеб" and len(P.clean_title("я" * 999)) == P.MAX_TITLE and P.clean_title(None) == ""
assert P.clean_time("07:30") == "07:30" and P.clean_time("7:30") is None and P.clean_time("24:00") is None and P.clean_time("12:60") is None
assert P.clean_priority(2) == 2 and P.clean_priority(9) == 4 and P.clean_priority(True) == 4 and P.clean_priority("1") == 4
assert P.clean_labels(["@Учёба", " учёба ", "а  б", 5, "", "x" * 99]) == ["учёба", "а б", "x" * 40]
assert len(P.clean_labels([f"l{i}" for i in range(50)])) == P.MAX_LABELS
assert P.clean_recur({"kind": "day", "every": 3}) == {"kind": "day", "every": 3}
assert P.clean_recur({"kind": "week", "every": 1, "days": [3, 1, 3]}) == {"kind": "week", "every": 1, "days": [1, 3]}
assert P.clean_recur({"kind": "week", "every": 1, "days": []}) == {"kind": "week", "every": 1}
for bad in (None, {}, {"kind": "year", "every": 1}, {"kind": "day", "every": 0}, {"kind": "day", "every": 366}, {"kind": "day", "every": "2"}, {"kind": "day", "every": True},
            {"kind": "week", "every": 1, "days": [7]}, {"kind": "week", "every": 1, "days": "1"}):
    assert P.clean_recur(bad) is None, bad
# 2026-10-07 — среда (в JS: 3)
assert P.js_weekday(date(2026, 10, 7)) == 3 and P.js_weekday(date(2026, 10, 11)) == 0 and P.js_weekday(date(2026, 10, 12)) == 1
assert P.occurs_on({"kind": "day", "every": 3}, "2026-10-07", "2026-10-13") and not P.occurs_on({"kind": "day", "every": 3}, "2026-10-07", "2026-10-12")
assert not P.occurs_on({"kind": "day", "every": 1}, "2026-10-07", "2026-10-06")  # до опорного дня — нет
wk = {"kind": "weekdays", "every": 1}
assert [P.occurs_on(wk, "2026-10-05", f"2026-10-{d:02d}") for d in range(5, 12)] == [True] * 5 + [False] * 2
tt = {"kind": "week", "every": 1, "days": [2, 4]}
assert [d for d in range(5, 12) if P.occurs_on(tt, "2026-10-05", f"2026-10-{d:02d}")] == [6, 8]
two = {"kind": "week", "every": 2, "days": [1]}
assert P.occurs_on(two, "2026-10-05", "2026-10-19") and P.occurs_on(two, "2026-10-05", "2026-11-02") and not P.occurs_on(two, "2026-10-05", "2026-10-12") and not P.occurs_on(two, "2026-10-05", "2026-10-26")
assert P.occurs_on({"kind": "week", "every": 1}, "2026-10-07", "2026-10-14") and not P.occurs_on({"kind": "week", "every": 1}, "2026-10-07", "2026-10-13")  # без дней — день опорной даты
m31 = {"kind": "month", "every": 1}
assert P.occurs_on(m31, "2026-01-31", "2026-02-28") and P.occurs_on(m31, "2026-01-31", "2026-04-30") and not P.occurs_on(m31, "2026-01-31", "2026-02-27")
assert P.occurs_on({"kind": "month", "every": 2}, "2026-01-15", "2026-03-15") and not P.occurs_on({"kind": "month", "every": 2}, "2026-01-15", "2026-02-15")
assert P.occurs_on({"kind": "month", "every": 1}, "2026-12-31", "2027-01-31")  # через границу года
# следующий срок — строго после сегодняшнего и текущего срока
every_day = {"kind": "day", "every": 1}
assert P.next_due(every_day, "2026-10-01", "2026-10-07") == "2026-10-08"      # просроченная: на завтра, без хвоста
assert P.next_due(every_day, "2026-10-07", "2026-10-07") == "2026-10-08"
assert P.next_due(every_day, "2026-10-20", "2026-10-07") == "2026-10-21"      # срок в будущем: после него
assert P.next_due(every_day, None, "2026-10-07") == "2026-10-08"
assert P.next_due(wk, "2026-10-09", "2026-10-09") == "2026-10-12"             # пятница → понедельник
assert P.next_due(tt, "2026-10-06", "2026-10-06") == "2026-10-08"
assert P.next_due(m31, "2026-01-31", "2026-01-31") == "2026-02-28"
assert P.first_due(wk, "2026-10-10") == "2026-10-12" and P.first_due(wk, "2026-10-07") == "2026-10-07"      # суббота → понедельник; среда — сегодня
assert P.first_due(tt, "2026-10-07") == "2026-10-08" and P.first_due(every_day, "2026-10-07") == "2026-10-07"
print("OK: чистая логика — очистка и повторы")

# ----------------------------------------------------------------------------- доступ
adm, au = reg("adm")
a, ua = reg("plana")
b, ub = reg("planb")
for method, path, body in (("GET", "/api/tasks", None), ("POST", "/api/tasks", {"title": "x"}), ("PUT", "/api/tasks/1", {"title": "x"}), ("POST", "/api/tasks/1/toggle", {}),
                           ("DELETE", "/api/tasks/1", None), ("DELETE", "/api/tasks/completed", None), ("POST", "/api/tasks/restore", {"tasks": []}),
                           ("POST", "/api/task-projects", {"name": "x"}), ("PUT", "/api/task-projects/1", {"name": "x"}), ("DELETE", "/api/task-projects/1", None),
                           ("GET", "/api/today", None), ("GET", f"/api/calendar?from={TODAY}&to={TODAY}", None)):
    r = httpx.request(method, BASE + path, json=body)
    assert r.status_code == 401, (method, path, r.status_code)
st = a.get("/api/tasks").json()
assert st["tasks"] == [] and st["projects"] == [] and st["limits"]["depth"] == 3

# ----------------------------------------------------------------------------- создание
t1 = a.post("/api/tasks", json={"title": "  Сдать   лабу  ", "priority": 1, "due": D(1), "due_time": "18:00", "labels": ["@Учёба", "учёба"], "note": "по сетям"}).json()
assert t1["title"] == "Сдать лабу" and t1["priority"] == 1 and t1["due"] == D(1) and t1["due_time"] == "18:00" and t1["labels"] == ["учёба"]
assert t1["done"] is False and t1["recur"] is None and t1["parent_id"] is None and t1["project_id"] is None and t1["note"] == "по сетям"
for body in ({"title": ""}, {"title": "x", "priority": 5}, {"title": "x", "priority": 0}, {"title": "x", "extra": 1}, {"title": "x" * 3001}, {}):
    assert a.post("/api/tasks", json=body).status_code == 422, body
assert err(a.post("/api/tasks", json={"title": "   "})) == "Название не может быть пустым"
assert err(a.post("/api/tasks", json={"title": "x", "due": "2026-02-30"})) == "Неверная дата"
assert err(a.post("/api/tasks", json={"title": "x", "due": TODAY, "due_time": "25:00"})).startswith("Неверное время")
assert err(a.post("/api/tasks", json={"title": "x", "recur": {"kind": "year", "every": 1}})) == "Неверное правило повтора"
assert a.post("/api/tasks", json={"title": "x", "project_id": 99999}).status_code == 404
assert a.post("/api/tasks", json={"title": "x", "parent_id": 99999}).status_code == 404
# время без срока отбрасывается; повтор без срока получает первый срок
t = a.post("/api/tasks", json={"title": "Без срока", "due_time": "09:00"}).json()
assert t["due"] is None and t["due_time"] is None
rd = a.post("/api/tasks", json={"title": "Планёрка", "recur": {"kind": "week", "every": 1, "days": [2, 4]}, "today": TODAY}).json()
assert rd["due"] == P.first_due({"kind": "week", "every": 1, "days": [2, 4]}, TODAY) and P.js_weekday(date.fromisoformat(rd["due"])) in (2, 4) and rd["recur"] == {"kind": "week", "every": 1, "days": [2, 4]}
# неправдоподобное «сегодня» браузера (не в пределах двух суток) заменяется серверным днём
rd2 = a.post("/api/tasks", json={"title": "Зарядка", "recur": {"kind": "day", "every": 1}, "today": "2001-01-01"}).json()
assert rd2["due"] == TODAY
rd3 = a.post("/api/tasks", json={"title": "Утро", "recur": {"kind": "day", "every": 1}, "today": D(1)}).json()
assert rd3["due"] == D(1)  # разница в сутки — часовой пояс человека, принимаем
print("OK: создание и проверки")

# ----------------------------------------------------------------------------- проекты, подзадачи
home = a.post("/api/task-projects", json={"name": "  Дом ", "emoji": "🏠"}).json()
assert home["name"] == "Дом" and home["emoji"] == "🏠"
assert a.post("/api/task-projects", json={"name": "дом"}).json()["id"] == home["id"]  # повторное имя — тот же проект
uni = a.post("/api/task-projects", json={"name": "Универ"}).json()
assert uni["emoji"] == "📁" and uni["order"] == home["order"] + 1
assert a.post("/api/task-projects", json={"name": "   "}).status_code == 400 and a.post("/api/task-projects", json={"name": ""}).status_code == 422
assert err(a.put(f"/api/task-projects/{uni['id']}", json={"name": "ДОМ"})) == "Проект с таким названием уже есть"
assert a.put(f"/api/task-projects/{uni['id']}", json={"name": "Учёба", "emoji": "🎓"}).json()["name"] == "Учёба"
assert b.put(f"/api/task-projects/{uni['id']}", json={"name": "Чужой"}).status_code == 404
assert b.post("/api/tasks", json={"title": "x", "project_id": uni["id"]}).status_code == 404  # чужой проект

p = a.post("/api/tasks", json={"title": "Генеральная уборка", "project_id": home["id"], "recur": {"kind": "month", "every": 1}}).json()
assert p["project_id"] == home["id"] and p["recur"]["kind"] == "month"
s1 = a.post("/api/tasks", json={"title": "Окна", "parent_id": p["id"], "project_id": uni["id"], "recur": {"kind": "day", "every": 1}}).json()
assert s1["parent_id"] == p["id"] and s1["project_id"] == home["id"] and s1["recur"] is None, "подзадача — в проекте родителя и без повтора"
s2 = a.post("/api/tasks", json={"title": "Шторы", "parent_id": s1["id"]}).json()
assert err(a.post("/api/tasks", json={"title": "Слишком глубоко", "parent_id": s2["id"]})).startswith("Вложенность")
print("OK: проекты и подзадачи")

# ----------------------------------------------------------------------------- правка
u = a.put(f"/api/tasks/{t1['id']}", json={"title": "Сдать лабу по сетям", "priority": 2, "labels": ["Сети"]}).json()
assert u["title"] == "Сдать лабу по сетям" and u["priority"] == 2 and u["labels"] == ["сети"] and u["due"] == D(1) and u["due_time"] == "18:00"
assert err(a.put(f"/api/tasks/{t1['id']}", json={"title": "  "})) == "Название не может быть пустым"
assert a.put(f"/api/tasks/{t1['id']}", json={"due": "bad"}).status_code == 400
assert a.put(f"/api/tasks/{t1['id']}", json={"priority": None}).status_code == 400
assert a.put(f"/api/tasks/{t1['id']}", json={"unknown": 1}).status_code == 422
u = a.put(f"/api/tasks/{t1['id']}", json={"due": None}).json()
assert u["due"] is None and u["due_time"] is None, "убрали срок — время тоже"
assert a.put(f"/api/tasks/{t1['id']}", json={"due": D(2), "due_time": "07:15", "note": ""}).json()["due_time"] == "07:15"
assert a.put(f"/api/tasks/{t1['id']}", json={"project_id": home["id"]}).json()["project_id"] == home["id"]
assert a.put(f"/api/tasks/{t1['id']}", json={"project_id": None}).json()["project_id"] is None
assert a.put(f"/api/tasks/{t1['id']}", json={"project_id": 99999}).status_code == 404
# перенос в другой проект тянет подзадачи; подзадачу в проект отдельно перенести нельзя
a.put(f"/api/tasks/{p['id']}", json={"project_id": uni["id"]})
kids = {x["id"]: x for x in a.get("/api/tasks").json()["tasks"]}
assert kids[p["id"]]["project_id"] == uni["id"] and kids[s1["id"]]["project_id"] == uni["id"] and kids[s2["id"]]["project_id"] == uni["id"]
assert a.put(f"/api/tasks/{s1['id']}", json={"project_id": home["id"]}).json()["project_id"] == uni["id"]
# нельзя вложить задачу в себя или в свою подзадачу; глубина ограничена
assert a.put(f"/api/tasks/{p['id']}", json={"parent_id": p["id"]}).status_code == 400
assert a.put(f"/api/tasks/{p['id']}", json={"parent_id": s2["id"]}).status_code == 400
assert a.put(f"/api/tasks/{t1['id']}", json={"parent_id": s2["id"]}).status_code == 400  # было бы 4 уровня
assert a.put(f"/api/tasks/{t1['id']}", json={"parent_id": s1["id"]}).json()["parent_id"] == s1["id"]  # 3 уровня — можно
assert a.put(f"/api/tasks/{t1['id']}", json={"parent_id": None}).json()["parent_id"] is None
# повтор: подзадаче нельзя; задаче без срока — ставится первый срок
r = a.put(f"/api/tasks/{s1['id']}", json={"recur": {"kind": "day", "every": 1}}).json()
assert r["recur"] is None
nt = a.post("/api/tasks", json={"title": "Спорт"}).json()
r = a.put(f"/api/tasks/{nt['id']}", json={"recur": {"kind": "weekdays", "every": 1}, "today": TODAY}).json()
assert r["due"] == P.first_due({"kind": "weekdays", "every": 1}, TODAY) and 1 <= P.js_weekday(date.fromisoformat(r["due"])) <= 5, "первый будний день"
assert a.put(f"/api/tasks/{nt['id']}", json={"recur": {"kind": "bad"}}).status_code == 400
assert a.put(f"/api/tasks/{nt['id']}", json={"recur": None}).json()["recur"] is None
# чужое недоступно
for call in (lambda: b.put(f"/api/tasks/{t1['id']}", json={"title": "взлом"}), lambda: b.post(f"/api/tasks/{t1['id']}/toggle", json={}), lambda: b.delete(f"/api/tasks/{t1['id']}")):
    assert call().status_code == 404
assert b.get("/api/tasks").json()["tasks"] == []
assert a.put("/api/tasks/987654", json={"title": "x"}).status_code == 404
print("OK: правка, перенос, вложенность, чужое недоступно")

# ----------------------------------------------------------------------------- выполнение
# обычная задача с подзадачами: закрытие закрывает подзадачи, возврат подзадачи открывает закрытого родителя
r = a.post(f"/api/tasks/{p['id']}/toggle", json={}).json()
# p — повторяющаяся (каждый месяц): не закрывается, а переезжает
assert r["rescheduled_to"] and r["task"]["done"] is False and r["task"]["due"] == r["rescheduled_to"] and r["rescheduled_to"] > TODAY
a.put(f"/api/tasks/{p['id']}", json={"recur": None})
r = a.post(f"/api/tasks/{p['id']}/toggle", json={}).json()
assert r["rescheduled_to"] is None and r["task"]["done"] is True and r["task"]["done_at"]
assert {x["id"] for x in r["changed"]} == {p["id"], s1["id"], s2["id"]} and all(x["done"] for x in r["changed"])
r = a.post(f"/api/tasks/{s2['id']}/toggle", json={}).json()  # вернули подзадачу — родители снова в работе
assert r["task"]["done"] is False and {x["id"] for x in r["changed"]} == {s2["id"], s1["id"], p["id"]} and not any(x["done"] for x in r["changed"])
# повторяющаяся: переезд на следующий срок, закрытые подзадачи открываются
rec = a.post("/api/tasks", json={"title": "Уборка стола", "recur": {"kind": "day", "every": 1}, "due": D(-3)}).json()
sub = a.post("/api/tasks", json={"title": "Протереть", "parent_id": rec["id"]}).json()
a.post(f"/api/tasks/{sub['id']}/toggle", json={})
r = a.post(f"/api/tasks/{rec['id']}/toggle", json={}).json()
assert r["rescheduled_to"] == D(1) and r["task"]["done"] is False, "просроченная ежедневная — на завтра"
assert any(x["id"] == sub["id"] and not x["done"] for x in r["changed"])
r = a.post(f"/api/tasks/{rec['id']}/toggle", json={"today": D(1)}).json()
assert r["rescheduled_to"] == D(2)
assert a.post("/api/tasks/987654/toggle", json={}).status_code == 404
assert a.post(f"/api/tasks/{rec['id']}/toggle", json={"today": "garbage"}).status_code == 200  # мусорный день игнорируется
print("OK: выполнение, повторы, подзадачи")

# ----------------------------------------------------------------------------- удаление и «Отменить»
before = {x["id"]: x for x in a.get("/api/tasks").json()["tasks"]}
d = a.delete(f"/api/tasks/{p['id']}").json()
assert {x["id"] for x in d["deleted"]} == {p["id"], s1["id"], s2["id"]}
after = {x["id"] for x in a.get("/api/tasks").json()["tasks"]}
assert not ({p["id"], s1["id"], s2["id"]} & after)
rs = a.post("/api/tasks/restore", json={"tasks": d["deleted"]}).json()
assert {x["id"] for x in rs["tasks"]} == {p["id"], s1["id"], s2["id"]}
back = {x["id"]: x for x in a.get("/api/tasks").json()["tasks"]}
for i in (p["id"], s1["id"], s2["id"]):
    assert back[i]["title"] == before[i]["title"] and back[i]["parent_id"] == before[i]["parent_id"] and back[i]["project_id"] == before[i]["project_id"], i
# восстановление изменённой (выполненной) задачи
snap = back[nt["id"]]
a.post(f"/api/tasks/{nt['id']}/toggle", json={})
assert a.post("/api/tasks/restore", json={"tasks": [snap]}).json()["tasks"][0]["done"] is False
# чужой номер не перезаписывается и не создаётся; мусор пропускается; циклы разрываются
tb = b.post("/api/tasks", json={"title": "Задача Б"}).json()
rs = a.post("/api/tasks/restore", json={"tasks": [dict(snap, id=tb["id"], title="захват"), {"id": "x"}, {"title": "без номера"}, dict(snap, id=tb["id"] + 1000, title="  ")]}).json()
assert rs["tasks"] == [] and b.get("/api/tasks").json()["tasks"][0]["title"] == "Задача Б"
ca, cb = back[p["id"]], back[s1["id"]]
a.post("/api/tasks/restore", json={"tasks": [dict(ca, parent_id=cb["id"]), dict(cb, parent_id=ca["id"])]})
now = {x["id"]: x for x in a.get("/api/tasks").json()["tasks"]}
assert not (now[ca["id"]]["parent_id"] == cb["id"] and now[cb["id"]]["parent_id"] == ca["id"]), "цикл разорван"
assert a.post("/api/tasks/restore", json={"tasks": [{"id": 1}] * 501}).status_code == 422
print("OK: удаление и «Отменить»")

# ----------------------------------------------------------------------------- очистка выполненных
c1 = a.post("/api/tasks", json={"title": "Родитель"}).json()
c2 = a.post("/api/tasks", json={"title": "Дитя", "parent_id": c1["id"]}).json()
a.post(f"/api/tasks/{c1['id']}/toggle", json={})   # закрыт вместе с дитём
a.post(f"/api/tasks/{c2['id']}/toggle", json={})   # дитя вернули в работу → родитель тоже открылся
a.post(f"/api/tasks/{c2['id']}/toggle", json={})   # снова закрыли только дитя
done_ids = {x["id"] for x in a.get("/api/tasks").json()["tasks"] if x["done"]}
assert c2["id"] in done_ids
n = a.delete("/api/tasks/completed").json()["deleted"]
assert n == len(done_ids) and not any(x["done"] for x in a.get("/api/tasks").json()["tasks"])
orph = a.post("/api/tasks", json={"title": "Сирота-родитель"}).json()
oc = a.post("/api/tasks", json={"title": "Сирота-дитя", "parent_id": orph["id"]}).json()
sql("UPDATE planner_tasks SET done=1, done_at=1 WHERE id=?", (orph["id"],))  # синхронизации нет, но состояние «родитель закрыт, дитя открыто» возможно
a.delete("/api/tasks/completed")
assert {x["id"]: x for x in a.get("/api/tasks").json()["tasks"]}[oc["id"]]["parent_id"] is None
print("OK: очистка выполненных")

# ----------------------------------------------------------------------------- проекты: удаление
before_n = len(a.get("/api/tasks").json()["tasks"])
x1 = a.post("/api/tasks", json={"title": "В проекте 1", "project_id": uni["id"]}).json()
r = a.delete(f"/api/task-projects/{uni['id']}").json()
assert r["deleted_tasks"] >= 1
rest = a.get("/api/tasks").json()
assert all(x["project_id"] != uni["id"] for x in rest["tasks"]) and uni["id"] not in {x["id"] for x in rest["projects"]}
assert a.delete(f"/api/task-projects/{uni['id']}").status_code == 404 and b.delete(f"/api/task-projects/{home['id']}").status_code == 404
print("OK: удаление проекта")

# ----------------------------------------------------------------------------- лимиты
sql("DELETE FROM planner_tasks WHERE user_id=?", (ub["id"],))
con = sqlite3.connect(db_path, timeout=30)
con.executemany("INSERT INTO planner_tasks(user_id,title,created_at,updated_at) VALUES(?,?,?,?)", [(ub["id"], f"т{i}", 1, 1) for i in range(P.MAX_TASKS)])
con.commit()
con.close()
assert "Не больше" in err(b.post("/api/tasks", json={"title": "лишняя"}))
sql("DELETE FROM planner_tasks WHERE user_id=?", (ub["id"],))
print("OK: лимит задач")

# ----------------------------------------------------------------------------- «Сегодня»: план по целям курсов
c, uc = reg("plnc")
empty = c.get(f"/api/today?today={TODAY}").json()
assert empty["items"] == [] and empty["required_total"] == 0 and empty["percent"] == 0 and empty["streak"]["current"] == 0


def seed_course(uid, title, goal=None, n_lessons=4, done=0, types=None):
    sql("INSERT INTO courses(user_id,title,icon,status,goal_date,goal_set_at) VALUES(?,?,?,?,?,?)", (uid, title, "📘", "ready", goal, TODAY if goal else None))
    cid = sql("SELECT MAX(id) FROM courses")[0][0]
    sql("INSERT INTO modules(course_id,idx,title,status) VALUES(?,?,?,?)", (cid, 0, "Модуль", "done" if done == n_lessons else "ready"))
    mid = sql("SELECT MAX(id) FROM modules")[0][0]
    lids = []
    for i in range(n_lessons):
        typ = (types or ["lesson"] * n_lessons)[i]
        sql("INSERT INTO lessons(course_id,module_id,idx,type,title,minutes,status) VALUES(?,?,?,?,?,?,?)", (cid, mid, i, typ, f"Урок {i + 1}", 10, "done" if i < done else "pending"))
        lids.append(sql("SELECT MAX(id) FROM lessons")[0][0])
    return cid, lids


# цель через 3 дня (сегодня + 2 + сегодняшний): 4 урока по 10 минут = 40 минут → ≈10 мин в день
g_cid, g_lessons = seed_course(uc["id"], "Сети", goal=D(3), n_lessons=4)
f_cid, _ = seed_course(uc["id"], "Без цели", n_lessons=3, done=1)
o_cid, _ = seed_course(uc["id"], "Опоздал", goal=D(-2), n_lessons=3)
c.post("/api/tasks", json={"title": "Просроченное", "due": D(-1)})
c.post("/api/tasks", json={"title": "На сегодня", "due": TODAY})
c.post("/api/tasks", json={"title": "На завтра", "due": D(1)})
c.post("/api/tasks", json={"title": "Без срока"})
pl = c.get(f"/api/today?today={TODAY}").json()
kinds = {i["kind"]: i for i in pl["items"]}
assert set(kinds) == {"goal", "overdue", "tasks", "lesson"}, [i["id"] for i in pl["items"]]
assert kinds["goal"]["course_id"] == g_cid and kinds["goal"]["required"] and not kinds["goal"]["done"] and kinds["goal"]["minutes"] == 10 and kinds["goal"]["goal"]["per_day_minutes"] == 10
assert kinds["overdue"]["course_id"] == o_cid and kinds["overdue"]["required"] and kinds["overdue"]["minutes"] == 30 and "срок прошёл" in kinds["overdue"]["detail"]
assert kinds["tasks"]["overdue"] == 1 and kinds["tasks"]["today"] == 1 and kinds["tasks"]["required"] and kinds["tasks"]["to"] == "#/tasks?v=today"
assert kinds["lesson"]["course_id"] == f_cid and kinds["lesson"]["next_title"] == "Урок 2" and kinds["lesson"]["required"] is False, "свободный курс не обязателен, раз есть другие дела"
assert pl["required_total"] == 3 and pl["required_done"] == 0 and pl["percent"] == 0 and pl["minutes_left"] == 40 and pl["tasks_tomorrow"] == 1
assert [i["kind"] for i in pl["items"]][:2] in (["goal", "overdue"], ["overdue", "goal"]) and pl["items"][-2]["kind"] == "tasks" and pl["items"][-1]["kind"] == "lesson"
# занялись сегодня на норму: цель выполнена
sql("INSERT INTO activity(user_id,course_id,day,xp,lessons,seconds,answers,correct) VALUES(?,?,?,?,?,?,?,?)", (uc["id"], g_cid, TODAY, 30, 1, 11 * 60, 5, 5))
sql("UPDATE lessons SET status='done' WHERE id=?", (g_lessons[0],))  # урок пройден: осталось 30 минут из 40, норма на день — 11
pl = c.get(f"/api/today?today={TODAY}").json()
goal_item = next(i for i in pl["items"] if i["kind"] == "goal")
assert goal_item["done"] and goal_item["minutes"] == 0 and pl["required_done"] == 1 and pl["percent"] == 33 and pl["streak"]["done_today"] and pl["minutes_left"] == 30
# задачи сделаны → пункт «Задачи» исчезает, считается в «сделано сегодня»
for tk in [x for x in c.get("/api/tasks").json()["tasks"] if x["due"] and x["due"] <= TODAY]:
    c.post(f"/api/tasks/{tk['id']}/toggle", json={})
pl = c.get(f"/api/today?today={TODAY}").json()
assert "tasks" not in {i["kind"] for i in pl["items"]} and pl["tasks_done_today"] == 2 and pl["required_total"] == 2
# только курс без цели: обязателен, пока сегодня уроков не было
d_, ud = reg("plnd")
seed_course(ud["id"], "Один", n_lessons=3)
one = d_.get(f"/api/today?today={TODAY}").json()
assert [i["kind"] for i in one["items"]] == ["lesson"] and one["items"][0]["required"] and one["percent"] == 0 and one["required_total"] == 1
sql("INSERT INTO activity(user_id,course_id,day,xp,lessons,seconds,answers,correct) VALUES(?,?,?,?,?,?,?,?)", (ud["id"], sql("SELECT id FROM courses WHERE user_id=?", (ud["id"],))[0][0], TODAY, 10, 1, 300, 3, 3))
one = d_.get(f"/api/today?today={TODAY}").json()
assert one["items"][0]["done"] and one["percent"] == 100 and one["required_total"] == 0
# чужие курсы и шаблоны не попадают
sql("UPDATE courses SET is_template=1 WHERE id=?", (f_cid,))
assert "lesson" not in {i["kind"] for i in c.get(f"/api/today?today={TODAY}").json()["items"]}
assert {i["kind"] for i in a.get(f"/api/today?today={TODAY}").json()["items"]} <= {"tasks"}  # у a курсов нет
assert c.get("/api/today?today=" + "9" * 11).status_code == 422
print("OK: план на день")

# ----------------------------------------------------------------------------- «Календарь»
k_cid, k_lessons = seed_course(uc["id"], "Календарный", goal=D(5), n_lessons=3, types=["lesson", "terms_test", "final_test"])
sql("INSERT INTO runs(user_id,lesson_id,finished_at,score) VALUES(?,?,datetime('now'),?)", (uc["id"], k_lessons[1], 0.8))
sql("INSERT INTO runs(user_id,lesson_id,finished_at,score) VALUES(?,?,datetime('now'),?)", (uc["id"], k_lessons[0], 1.0))       # не тест — не показываем
sql("INSERT INTO runs(user_id,lesson_id,finished_at,score) VALUES(?,?,NULL,NULL)", (uc["id"], k_lessons[2]))                      # не закончен — не показываем
cal = c.get(f"/api/calendar?from={D(-10)}&to={D(10)}").json()
assert {g["course_id"] for g in cal["goals"]} == {g_cid, o_cid, k_cid} and next(g for g in cal["goals"] if g["course_id"] == k_cid)["date"] == D(5)
assert [g["date"] for g in cal["goals"]] == sorted(g["date"] for g in cal["goals"])
assert len(cal["tests"]) == 1 and cal["tests"][0]["type"] == "terms_test" and cal["tests"][0]["score"] == 80 and cal["tests"][0]["course_title"] == "Календарный" and cal["tests"][0]["type_name"] == "Тест по терминам"
assert cal["tests"][0]["day"] in (TODAY, D(-1), D(1))  # серверное «локальное» время ≈ сегодня
assert cal["study"] == [{"day": TODAY, "minutes": 11, "lessons": 1, "xp": 30}]
narrow = c.get(f"/api/calendar?from={D(4)}&to={D(6)}").json()
assert [g["course_id"] for g in narrow["goals"]] == [k_cid] and narrow["tests"] == [] and narrow["study"] == []
assert a.get(f"/api/calendar?from={D(-10)}&to={D(10)}").json()["goals"] == [] and a.get(f"/api/calendar?from={D(-10)}&to={D(10)}").json()["tests"] == []  # чужое не видно
assert err(c.get(f"/api/calendar?from={D(5)}&to={D(1)}")) == "Неверный период"
assert err(c.get("/api/calendar?from=2026-02-30&to=2026-03-02")) == "Неверный период"
assert "не больше" in err(c.get(f"/api/calendar?from={D(0)}&to={D(71)}"))
assert c.get(f"/api/calendar?from={D(0)}&to={D(70)}").status_code == 200
assert c.get("/api/calendar").status_code == 422
print("OK: календарь")

# ----------------------------------------------------------------------------- удаление аккаунта
assert sql("SELECT COUNT(*) FROM planner_tasks WHERE user_id=?", (ua["id"],))[0][0] > 0 and sql("SELECT COUNT(*) FROM planner_projects WHERE user_id=?", (ua["id"],))[0][0] > 0
assert adm.delete(f"/api/admin/users/{ua['id']}").status_code == 200
assert sql("SELECT COUNT(*) FROM planner_tasks WHERE user_id=?", (ua["id"],))[0][0] == 0 and sql("SELECT COUNT(*) FROM planner_projects WHERE user_id=?", (ua["id"],))[0][0] == 0
assert sql("SELECT COUNT(*) FROM planner_tasks WHERE user_id=?", (uc["id"],))[0][0] > 0, "чужие задачи на месте"
print("OK: удаление аккаунта чистит планировщик")
print("ВСЕ ПРОВЕРКИ ПЛАНИРОВЩИКА ПРОЙДЕНЫ")
