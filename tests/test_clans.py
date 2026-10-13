"""Кланы: создание и проверка ввода, уникальность названия и тега, вступление и выход, лидерство, кик, роспуск, рейтинг, профили, чат,
лимиты (участники, создание за сутки), заблокированные и удалённые игроки.
Запуск (чистый сервер, LQ_DB — путь к его базе): LQ_DB=<tmp.db> python tests/test_clans.py http://127.0.0.1:8011"""
import os
import sqlite3
import sys
import time
from datetime import date, timedelta

import httpx

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
from server import clans as C  # noqa: E402

db_path = os.environ.get("LQ_DB")
SUFFIX = str(int(time.time()) % 100000)


def sql(q, args=()):
    con = sqlite3.connect(db_path, timeout=30)
    try:
        cur = con.execute(q, args)
        con.commit()
        return cur.fetchall()
    finally:
        con.close()


def reg(name):
    d = httpx.post(f"{BASE}/api/auth/register", json={"username": f"{name}{SUFFIX}", "password": "secret1"}, timeout=30).json()
    return httpx.Client(base_url=BASE, timeout=60, headers={"Authorization": f"Bearer {d['token']}"}), d["user"]


def make(cli, name="Волки Севера", tag="vlk", **kw):
    return cli.post("/api/clans", json={"name": name, "tag": tag, **kw})


def ids(resp):
    return [c["id"] for c in resp.json()["clans"]]


# ----------------------------------------------------------------------------- правила ввода
assert C.clean_name("  Волки   Севера ") == "Волки Севера" and C.clean_tag(" vlk ") == "VLK" and C.clean_desc("a\n b") == "a b"
for bad in ("ab", "x" * 25, "<b>", "-abc", "abc-", "a" * 3 + "!", "Клан😀"):
    try:
        C.clean_name(bad)
        raise AssertionError("название должно отклоняться: " + bad)
    except Exception as e:  # noqa: BLE001
        assert getattr(e, "status_code", None) == 400, (bad, e)
for bad in ("a", "abcdef", "a b", "ab!", "<>"):
    try:
        C.clean_tag(bad)
        raise AssertionError("тег должен отклоняться: " + bad)
    except Exception as e:  # noqa: BLE001
        assert getattr(e, "status_code", None) == 400, (bad, e)
print("OK: проверка названия, тега и описания")

# ----------------------------------------------------------------------------- доступ
assert httpx.get(BASE + "/api/clans").status_code == 401
assert httpx.post(BASE + "/api/clans", json={"name": "Волки", "tag": "vlk"}).status_code == 401
assert httpx.get(BASE + "/api/clans/mine").status_code == 401

adm, au = reg("cadm")     # первый пользователь тестового сервера — администратор
a, ua = reg("clana")
b, ub = reg("clanb")
c, uc = reg("clanc")
d, ud = reg("cland")
e, ue = reg("clane")

lst = a.get("/api/clans").json()
assert lst["clans"] == [] and lst["mine"] is None and lst["can_create"] is True and lst["max_members"] == C.MEMBERS_MAX and C.DEFAULT_EMOJI in lst["emojis"]
assert a.get("/api/clans/mine").json() == {"clan": None}
assert a.get("/api/clans/999999").status_code == 404

# ----------------------------------------------------------------------------- создание и проверка ввода
for body in ({"name": "ab", "tag": "vlk"}, {"name": "<script>", "tag": "vlk"}, {"name": "Волки", "tag": "v"}, {"name": "Волки", "tag": "v l"},
             {"name": "Волки", "tag": "abcdef"}, {"name": "Волки", "tag": "vlk", "emoji": "💩"}, {"name": "Волки", "tag": "vlk", "description": "я" * 81},
             {"name": "Волки", "tag": "vlk", "extra": 1}):
    assert a.post("/api/clans", json=body).status_code in (400, 422), body
assert a.get("/api/clans").json()["clans"] == [], "ошибочные запросы клан не создают"

r = make(a, name="Волки  Севера", tag="vlk", emoji="🐺", description="  Охотимся\nвместе ")
assert r.status_code == 200, r.text
cl = r.json()
cid = cl["id"]
assert cl["name"] == "Волки Севера" and cl["tag"] == "VLK" and cl["emoji"] == "🐺" and cl["description"] == "Охотимся вместе"
assert cl["members"] == 1 and cl["rank"] == 1 and cl["leader"]["id"] == ua["id"] and cl["max_members"] == C.MEMBERS_MAX
assert [m["id"] for m in cl["members_list"]] == [ua["id"]] and cl["members_list"][0]["role"] == "leader"
assert cl["me"] == {"in_this": True, "in_other": False, "is_leader": True, "can_manage": True, "is_admin": False}
assert a.get("/api/clans/mine").json()["clan"]["id"] == cid

# уникальность без учёта регистра (в том числе кириллица) и один клан на игрока
assert make(b, name="ВОЛКИ севера", tag="zzz").status_code == 400
assert make(b, name="Волки Юга", tag="Vlk").status_code == 400
assert make(b, name="ВОЛКИ  СЕВЕРА", tag="zzz").status_code == 400
assert make(a, name="Другой клан", tag="oth").status_code == 400, "уже в клане"
assert a.get("/api/clans").json()["can_create"] is False
print("OK: создание, проверка ввода, уникальность, один клан на игрока")

# ----------------------------------------------------------------------------- вступление
assert b.post(f"/api/clans/{cid}/join").status_code == 200
assert b.post(f"/api/clans/{cid}/join").status_code == 400, "повторное вступление"
assert make(b, name="Клан Б", tag="bbb").status_code == 400, "участник не может создать клан"
assert c.post("/api/clans/999999/join").status_code == 404
assert c.post(f"/api/clans/{cid}/join").status_code == 200
r2 = make(d, name="Орлы", tag="orl", emoji="🦅")
assert r2.status_code == 200
cid2 = r2.json()["id"]
assert c.post(f"/api/clans/{cid2}/join").status_code == 400, "нельзя быть сразу в двух кланах"
det = b.get(f"/api/clans/{cid}").json()
assert det["members"] == 3 and det["me"]["in_this"] and not det["me"]["is_leader"] and not det["me"]["can_manage"]
assert {m["id"]: m["role"] for m in det["members_list"]} == {ua["id"]: "leader", ub["id"]: "member", uc["id"]: "member"}
assert b.get(f"/api/clans/{cid2}").json()["me"] == {"in_this": False, "in_other": True, "is_leader": False, "can_manage": False, "is_admin": False}
assert e.get(f"/api/clans/{cid}").json()["me"]["in_this"] is False
print("OK: вступление, один клан на игрока")

# ----------------------------------------------------------------------------- клан в профилях, друзьях и чате
me_ = b.get("/api/me").json()
assert me_["clan"] == {"id": cid, "name": "Волки Севера", "tag": "VLK", "emoji": "🐺"}
assert e.get("/api/me").json()["clan"] is None
assert a.post("/api/friends/request", json={"username": ub["username"]}).status_code == 200
assert b.post(f"/api/friends/{ua['id']}/accept").status_code == 200
assert a.get(f"/api/friends/{ub['id']}").json()["clan"]["tag"] == "VLK"
fr = a.get("/api/friends").json()["friends"]
assert fr[0]["id"] == ub["id"] and fr[0]["clan"]["tag"] == "VLK"
m = b.post("/api/chat/messages", json={"text": "всем привет", "request_id": "clan-chat-req-0001"})
assert m.status_code == 200 and m.json()["clan_tag"] == "VLK", m.text
hist = a.get("/api/chat/messages").json()["messages"]
assert [x["clan_tag"] for x in hist if x["user_id"] == ub["id"]] == ["VLK"]
m2 = e.post("/api/chat/messages", json={"text": "я без клана", "request_id": "clan-chat-req-0002"}).json()
assert m2["clan_tag"] is None
print("OK: клан в /api/me, у друзей и в чате")

# ----------------------------------------------------------------------------- рейтинг: XP за неделю и всего
today, old = date.today().isoformat(), (date.today() - timedelta(days=30)).isoformat()
sql("INSERT INTO activity(user_id, course_id, day, xp, lessons) VALUES(?,?,?,?,1)", (ub["id"], 0, today, 500))
sql("INSERT INTO activity(user_id, course_id, day, xp, lessons) VALUES(?,?,?,?,1)", (ud["id"], 0, old, 10000))
by_week = a.get("/api/clans?sort=week").json()["clans"]
assert [x["id"] for x in by_week] == [cid, cid2] and by_week[0]["week_xp"] == 500 and by_week[0]["total_xp"] == 500 and by_week[0]["rank"] == 1
assert by_week[1]["week_xp"] == 0 and by_week[1]["total_xp"] == 10000 and by_week[1]["rank"] == 2
assert ids(a.get("/api/clans?sort=total")) == [cid2, cid]
assert ids(a.get("/api/clans?sort=members")) == [cid, cid2]
assert ids(a.get("/api/clans?sort=new")) == [cid2, cid]
assert a.get("/api/clans?sort=junk").status_code == 422
assert ids(a.get("/api/clans?q=ОРЛ")) == [cid2], "поиск по названию без учёта регистра (кириллица)"
assert ids(a.get("/api/clans?q=vl")) == [cid], "поиск по тегу"
assert ids(a.get("/api/clans?q=нет-такого")) == []
members = {m["id"]: m for m in b.get(f"/api/clans/{cid}").json()["members_list"]}
assert members[ub["id"]]["week_xp"] == 500 and members[ub["id"]]["xp"] == 500 and members[ua["id"]]["week_xp"] == 0
assert b.get(f"/api/clans/{cid}").json()["members_list"][0]["id"] == ub["id"], "участники по XP за неделю"
print("OK: рейтинг и поиск")

# ----------------------------------------------------------------------------- лидер: кик, описание, передача
assert b.post(f"/api/clans/{cid}/kick/{uc['id']}").status_code == 403, "обычный участник не исключает"
assert b.put(f"/api/clans/{cid}", json={"description": "взлом"}).status_code == 403
assert b.delete(f"/api/clans/{cid}").status_code == 403
assert a.post(f"/api/clans/{cid}/kick/{ua['id']}").status_code == 400, "лидера исключить нельзя"
assert a.post(f"/api/clans/{cid}/kick/{ue['id']}").status_code == 404, "не участник"
assert a.post(f"/api/clans/{cid2}/kick/{ud['id']}").status_code == 403, "чужой клан"
r = a.post(f"/api/clans/{cid}/kick/{uc['id']}")
assert r.status_code == 200 and r.json()["members"] == 2
assert c.get("/api/clans/mine").json() == {"clan": None}
assert a.put(f"/api/clans/{cid}", json={"emoji": "💩"}).status_code == 400
assert a.put(f"/api/clans/{cid}", json={"description": "я" * 81}).status_code == 400
r = a.put(f"/api/clans/{cid}", json={"emoji": "🐉", "description": "Новое описание"})
assert r.status_code == 200 and r.json()["emoji"] == "🐉" and r.json()["description"] == "Новое описание"
assert a.put(f"/api/clans/{cid}", json={"description": ""}).json()["description"] == ""
assert a.put(f"/api/clans/{cid}", json={"name": "Другое"}).status_code == 422, "название после создания не меняется"

assert b.post(f"/api/clans/{cid}/leader/{ub['id']}").status_code == 403
assert a.post(f"/api/clans/{cid}/leader/{ua['id']}").status_code == 400
assert a.post(f"/api/clans/{cid}/leader/{ue['id']}").status_code == 404
r = a.post(f"/api/clans/{cid}/leader/{ub['id']}")
assert r.status_code == 200 and r.json()["leader"]["id"] == ub["id"] and r.json()["me"]["is_leader"] is False
assert {m["id"]: m["role"] for m in r.json()["members_list"]} == {ua["id"]: "member", ub["id"]: "leader"}
assert a.put(f"/api/clans/{cid}", json={"description": "x"}).status_code == 403, "бывший лидер больше не управляет"
print("OK: кик, описание, передача лидерства")

# ----------------------------------------------------------------------------- выход и роспуск
assert e.post("/api/clans/leave").status_code == 400, "не в клане"
r = a.post("/api/clans/leave")
assert r.status_code == 200 and r.json() == {"ok": True, "disbanded": False}
assert c.post(f"/api/clans/{cid}/join").status_code == 200
assert b.post("/api/clans/leave").json() == {"ok": True, "disbanded": False}, "лидер уходит, клан остаётся"
d1 = c.get(f"/api/clans/{cid}").json()
assert d1["leader"]["id"] == uc["id"] and d1["members"] == 1 and d1["me"]["is_leader"], "лидерство перешло к оставшемуся"
assert c.post("/api/clans/leave").json() == {"ok": True, "disbanded": True}
assert c.get(f"/api/clans/{cid}").status_code == 404 and a.get("/api/clans/mine").json() == {"clan": None}
assert sql("SELECT COUNT(*) FROM clan_members WHERE clan_id=?", (cid,)) == [(0,)]
# название и тег освободились
assert make(a, name="Волки Севера", tag="vlk").status_code == 200
print("OK: выход, смена лидера при выходе, роспуск пустого клана")

# администратор распускает чужой клан; обычный лидер — свой
assert e.delete(f"/api/clans/{cid2}").status_code == 403
r = adm.get(f"/api/clans/{cid2}").json()
assert r["me"]["can_manage"] is True and r["me"]["is_admin"] is True and r["me"]["is_leader"] is False
assert adm.put(f"/api/clans/{cid2}", json={"description": "модерация"}).json()["description"] == "модерация"
assert adm.delete(f"/api/clans/{cid2}").status_code == 200
assert d.get("/api/clans/mine").json() == {"clan": None} and d.get("/api/me").json()["clan"] is None
mine = a.get("/api/clans/mine").json()["clan"]
assert a.delete(f"/api/clans/{mine['id']}").status_code == 200 and a.get("/api/clans/mine").json() == {"clan": None}
print("OK: роспуск лидером и администратором")

# ----------------------------------------------------------------------------- лимит участников
big = make(d, name="Большой клан", tag="big").json()["id"]
first = sql("SELECT MAX(id) FROM users")[0][0] + 1000
for i in range(C.MEMBERS_MAX - 1):
    uid = first + i
    sql("INSERT INTO users(id, username, display_name, pass_hash, salt) VALUES(?,?,?,?,?)", (uid, f"fake{SUFFIX}_{i}", f"fake{i}", "x", "x"))
    sql("INSERT INTO clan_members(user_id, clan_id, role) VALUES(?,?, 'member')", (uid, big))
assert d.get(f"/api/clans/{big}").json()["members"] == C.MEMBERS_MAX
r = e.post(f"/api/clans/{big}/join")
assert r.status_code == 400 and "заполнен" in r.json()["detail"], r.text
assert ids(e.get("/api/clans?q=большой")) == [big]
# одного из участников блокируют: он пропадает из состава и освобождает место
sql("UPDATE users SET banned=1 WHERE id=?", (first,))
assert d.get(f"/api/clans/{big}").json()["members"] == C.MEMBERS_MAX - 1
assert e.post(f"/api/clans/{big}/join").status_code == 200
assert e.post("/api/clans/leave").json()["disbanded"] is False
print("OK: лимит участников, заблокированные не считаются")

# ----------------------------------------------------------------------------- удаление аккаунта лидера, блокировка
assert adm.post(f"/api/admin/users/{ud['id']}/ban", json={"reason": "тест"}).status_code == 200
assert adm.post(f"/api/admin/users/{ud['id']}/unban").status_code == 200
assert adm.delete(f"/api/admin/users/{ud['id']}").status_code == 200
big_det = e.get(f"/api/clans/{big}").json()
assert big_det["leader"]["id"] != ud["id"] and big_det["members"] == C.MEMBERS_MAX - 2, "лидерство перешло к самому давнему участнику"
assert sql("SELECT COUNT(*) FROM clan_members WHERE user_id=?", (ud["id"],)) == [(0,)]
x, ux = reg("clanx")
y, uy = reg("clany")
cx = make(x, name="Икс Клан", tag="ixx").json()["id"]
assert y.post(f"/api/clans/{cx}/join").status_code == 200
assert adm.delete(f"/api/admin/users/{ux['id']}").status_code == 200
dx = y.get(f"/api/clans/{cx}").json()
assert dx["leader"]["id"] == uy["id"] and dx["members"] == 1 and dx["me"]["is_leader"]
assert y.post("/api/clans/leave").json()["disbanded"] is True
print("OK: удаление аккаунта лидера")

# ----------------------------------------------------------------------------- лимит создания клана за сутки
z, uz = reg("clanz")
for i in range(C.CREATE_LIMIT):
    r = make(z, name=f"Серия {i} Клан", tag=f"sr{i}")
    assert r.status_code == 200, r.text
    assert z.post("/api/clans/leave").json()["disbanded"] is True
r = make(z, name="Лишний Клан", tag="lsh")
assert r.status_code == 429, r.text
print("OK: лимит создания кланов за сутки")

# ----------------------------------------------------------------------------- администратор не получает клан сам по себе; изоляция ответов
assert adm.get("/api/me").json()["clan"] is None
print("OK: кланы — все проверки")
