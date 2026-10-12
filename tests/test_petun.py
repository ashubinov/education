"""ОТПЕТУНИВАНИЕ (API, сквозной): запускается через tests/run_ci.py на одноразовой базе LQ_DB.

Проверяет: выбор друга, одна неразгаданная атака у нападающего и у жертвы, «раз в 5 минут» на каждого игрока, анонимность до ответа,
угадал → ничего, не угадал → минута блокировки (на сервере доступны только курсы, профиль и выход), таймаут → блокировка при следующем входе,
идемпотентность и доступ.
"""
import os
import sqlite3
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
TAG = str(time.time_ns() % 10**11)
DBPATH = os.environ.get("LQ_DB")


def reg(suffix):
    username = f"pet{suffix}{TAG}"
    res = httpx.post(BASE + "/api/auth/register", json={"username": username, "password": "secret1"}, timeout=30)
    assert res.status_code == 200, res.text
    data = res.json()
    return httpx.Client(base_url=BASE, headers={"Authorization": "Bearer " + data["token"]}, timeout=30), data["user"]


def friends(a, aa, b, bb):
    assert a.post("/api/friends/request", json={"username": bb["username"]}).status_code == 200
    assert b.post(f"/api/friends/{aa['id']}/accept").status_code == 200


def alter(sql, args=()):
    assert DBPATH, "нужна отдельная тестовая база LQ_DB"
    with sqlite3.connect(DBPATH, timeout=30) as conn:
        conn.execute(sql, args)


def rid(n):
    return f"petun-request-{n:012d}"


def attack(client, victim_id, n):
    return client.post("/api/minigames/petun/attack", json={"victim_id": victim_id, "request_id": rid(n)})


def guess(client, aid, suspect, n):
    return client.post("/api/minigames/petun/guess", json={"attack_id": aid, "suspect_id": suspect, "request_id": rid(n)})


def chat_texts(client):
    return [m["text"] for m in client.get("/api/chat/messages").json()["messages"] if m.get("system_kind") == "strawberry"]


def pending(client):
    return client.get("/api/minigames/petun/state").json()["pending"]


thief, t = reg("thief")
victim, v = reg("victim")
other, o = reg("other")
third, x = reg("third")
outsider, u = reg("outsider")
friends(thief, t, victim, v)
friends(victim, v, other, o)
friends(thief, t, other, o)
friends(third, x, victim, v)

# --- доступ и простые отказы
assert httpx.post(BASE + "/api/minigames/petun/attack", json={"victim_id": v["id"], "request_id": rid(1)}).status_code == 401
assert attack(outsider, v["id"], 1).status_code == 404            # не друг
assert attack(thief, t["id"], 2).status_code == 400               # сам себя
assert thief.post("/api/minigames/petun/attack", json={"victim_id": v["id"], "request_id": "short"}).status_code == 422
st = thief.get("/api/minigames/petun/state").json()
assert {f["id"] for f in st["friends"]} == {v["id"], o["id"]} and all(f["wait"] == 0 and not f["busy"] for f in st["friends"])
assert st["lock_seconds"] == 60 and st["cooldown_seconds"] == 300

# --- атака: жертва видит только список подозреваемых
r = attack(thief, v["id"], 10)
assert r.status_code == 200 and not r.json()["replayed"], r.text
aid = r.json()["id"]
assert attack(thief, v["id"], 10).json()["replayed"] is True
assert attack(thief, o["id"], 10).status_code == 409                  # тот же request_id для другой цели
assert victim.get("/api/minigames/petun/status").json() == {"pending_count": 1, "latest_pending_id": aid, "latest_pending_amount": 0}
start = [x for x in chat_texts(third) if "ТЕБЯ ПЕТУНЯТ ПРЯМО СЕЙЧАС!!!!!" in x]
assert start and v["username"].upper() in start[-1] and t["username"].upper() not in start[-1], start   # в чате — только жертва
p = pending(victim)[0]
assert p["id"] == aid and {c["id"] for c in p["candidates"]} == {t["id"], o["id"], x["id"]}
assert "thief_id" not in p and "thief_username" not in p

# --- пока друг не ответил, следующего отпетунить нельзя; и жертву второй раз тоже
r = attack(thief, o["id"], 11); assert r.status_code == 409, r.text
assert "Сначала дождись" in r.json()["detail"]
r = attack(other, v["id"], 12); assert r.status_code == 409, r.text
assert thief.get("/api/minigames/petun/state").json()["has_active"] is True

# --- не угадал: минута блокировки; на сервере доступны только курсы и профиль
assert guess(outsider, aid, t["id"], 20).status_code == 404
assert guess(victim, aid, u["id"], 21).status_code == 422             # не из списка
g = guess(victim, aid, o["id"], 22)
assert g.status_code == 200, g.text
j = g.json()
assert j["caught"] is False and j["lock"] and 55 <= j["lock"]["seconds_left"] <= 60, j
assert guess(victim, aid, t["id"], 23).status_code == 423               # попытка одна, а отпетуненному игры недоступны (минуту — только курсы)
assert any(x == f"{v['username'].upper()} ОТПЕТУНИН ПО ПОЛНОЙ!!!!!" for x in chat_texts(third)), chat_texts(third)
me = victim.get("/api/me")
assert me.status_code == 200 and me.json()["petun"]["seconds_left"] > 50
assert victim.get("/api/courses").status_code == 200
assert victim.get("/api/stats").status_code == 200                    # главный экран курсов строится на статистике
for path in ("/api/slots/state", "/api/friends", "/api/pet", "/api/minigames/strawberry/state", "/api/minigames/petun/state", "/api/chat/messages", "/api/tasks"):
    rr = victim.get(path)
    assert rr.status_code == 423 and "ОТПЕТУНИН" in rr.json()["detail"], (path, rr.status_code)
assert victim.put("/api/me", json={"sound": False}).status_code == 423
assert thief.get("/api/slots/state").status_code == 200                # остальных блокировка не касается

# --- отпетуненного и «свежеотпетуненного» снова отпетунить нельзя
r = attack(thief, v["id"], 30); assert r.status_code == 409 and "отпетунен" in r.json()["detail"], r.text

# --- по истечении минуты всё снова работает
alter("UPDATE petun_locks SET until_ts=? WHERE user_id=?", (int(time.time()) - 1, v["id"]))
assert victim.get("/api/slots/state").status_code == 200
assert victim.get("/api/me").json()["petun"] is None

# --- «раз в 5 минут» на каждого игрока, кем бы ни был нападающий
r = attack(thief, v["id"], 31); assert r.status_code == 429 and "мин." in r.json()["detail"], r.text
r = attack(third, v["id"], 32); assert r.status_code == 429, r.text
st = thief.get("/api/minigames/petun/state").json()
fv = next(f for f in st["friends"] if f["id"] == v["id"])
assert 0 < fv["wait"] <= 300 and not st["has_active"]
assert [(a["caught"]) for a in st["recent"]] == [False]
# другого игрока отпетунить можно сразу (ограничение — на жертву, а не на нападающего)
r = attack(thief, o["id"], 33); assert r.status_code == 200, r.text
aid_o = r.json()["id"]
assert attack(thief, o["id"], 34).status_code == 409                    # у нападающего уже есть неразгаданная
# жертва угадала — ничего не происходит
g = guess(other, aid_o, t["id"], 35)
assert g.status_code == 200 and g.json()["caught"] is True and g.json()["lock"] is None, g.text
assert other.get("/api/slots/state").status_code == 200 and other.get("/api/me").json()["petun"] is None
assert thief.get("/api/minigames/petun/state").json()["recent"][0]["caught"] is True
assert f"{t['username'].upper()} СЛОВИЛ КРЕМПАЙ ОТ {o['username'].upper()}!!!!" in chat_texts(third), chat_texts(third)

# --- прошло 5 минут: отпетунить того же игрока снова можно
alter("UPDATE petun_attacks SET created_ts=created_ts-400 WHERE victim_id=?", (v["id"],))
r = attack(third, v["id"], 36); assert r.status_code == 200, r.text
aid_v2 = r.json()["id"]
assert guess(victim, aid_v2, x["id"], 37).json()["caught"] is True

# --- не ответил за час: «не угадал», блокировка начинается, когда жертва откроет сайт
alter("UPDATE petun_attacks SET created_ts=created_ts-400 WHERE victim_id=?", (o["id"],))
r = attack(thief, o["id"], 38); assert r.status_code == 200, r.text
aid_o2 = r.json()["id"]
alter("UPDATE petun_attacks SET expires_ts=? WHERE id=?", (int(time.time()) - 1, aid_o2))
assert other.get("/api/minigames/petun/status").json()["pending_count"] == 0       # просрочено (запрос списка сам закрывает атаку)
assert other.get("/api/slots/state").status_code == 200, "минута ещё не началась: жертва не открывала сайт"
lock = other.get("/api/me").json()["petun"]                                         # открыла сайт → минута пошла
assert lock and 55 <= lock["seconds_left"] <= 60, lock
assert other.get("/api/slots/state").status_code == 423
assert other.get("/api/me").json()["petun"]["until"] == lock["until"], "повторное открытие не продлевает минуту"
assert chat_texts(third).count(f"{o['username'].upper()} ОТПЕТУНИН ПО ПОЛНОЙ!!!!!") == 1, "таймаут тоже объявляется в чате"
assert guess(other, aid_o2, t["id"], 39).status_code == 423                          # отпетуненному игры недоступны, а отвечать уже поздно

# =============================== воровство жетонов (как у клубничного вора) ===============================
cthief, ct = reg("cthief")
cvictim, cv = reg("cvictim")
cother, co = reg("cother")
friends(cthief, ct, cvictim, cv)
friends(cvictim, cv, cother, co)


def bal(client):
    return client.get("/api/slots/state").json()["balance"]


def steal(client, victim_id, n, amount):
    return client.post("/api/minigames/petun/attack", json={"victim_id": victim_id, "amount": amount, "request_id": rid(n)})


def free_victim():
    """Снять блокировку и «прошло 5 минут» у жертвы."""
    alter("DELETE FROM petun_locks WHERE user_id=?", (cv["id"],))
    alter("UPDATE petun_attacks SET created_ts=created_ts-400 WHERE victim_id=?", (cv["id"],))


assert bal(cthief) == 1000 and bal(cvictim) == 1000
st = cthief.get("/api/minigames/petun/state").json()
assert st["friends"][0]["max_steal"] == 300 and st["max_steal_percent"] == 30 and st["catch_penalty_percent"] == 10 and st["frozen"] == 0
for bad in (301, -1, 10**12):
    r = steal(cthief, cv["id"], 200, bad)
    assert r.status_code in (400, 422), (bad, r.status_code, r.text)
assert bal(cvictim) == 1000

# жетоны замораживаются сразу, у нападающего не списываются и не прибавляются
r = steal(cthief, cv["id"], 201, 100)
assert r.status_code == 200 and r.json()["amount"] == 100, r.text
cid = r.json()["id"]
assert bal(cvictim) == 900 and bal(cthief) == 1000
assert steal(cthief, cv["id"], 201, 50).status_code == 409                    # тот же request_id с другой суммой
assert steal(cthief, cv["id"], 201, 100).json()["replayed"] is True and bal(cvictim) == 900
assert cthief.get("/api/minigames/petun/state").json()["frozen"] == 100
assert cvictim.get("/api/minigames/petun/status").json()["latest_pending_amount"] == 100
assert pending(cvictim)[0]["amount"] == 100

# не угадал: добыча вору, жертва отпетунена
g = guess(cvictim, cid, co["id"], 202)
assert g.status_code == 200 and g.json()["caught"] is False and g.json()["amount"] == 100 and g.json()["lock"], g.text
assert bal(cthief) == 1100 and cvictim.get("/api/me").status_code == 200
free_victim()
assert bal(cvictim) == 900

# угадал: жертва получает украденное и штраф 10 % с вора
r = steal(cthief, cv["id"], 203, 50)
assert r.status_code == 200, r.text
assert bal(cvictim) == 850
g = guess(cvictim, r.json()["id"], ct["id"], 204)
assert g.status_code == 200 and g.json()["caught"] is True and g.json()["compensation"] == 5 and g.json()["lock"] is None, g.text
assert bal(cvictim) == 905 and bal(cthief) == 1095 and bal(cthief) + bal(cvictim) == 2000, (bal(cthief), bal(cvictim))
assert cthief.get("/api/minigames/petun/state").json()["recent"][0]["penalty"] == 5
free_victim()

# не ответил за час: добыча вору, у жертвы блокировка при следующем входе
r = steal(cthief, cv["id"], 205, 40)
assert r.status_code == 200, r.text
alter("UPDATE petun_attacks SET expires_ts=? WHERE id=?", (int(time.time()) - 1, r.json()["id"]))
assert cvictim.get("/api/minigames/petun/status").json()["pending_count"] == 0
assert bal(cthief) == 1135 and bal(cvictim) == 865 and bal(cthief) + bal(cvictim) == 2000, (bal(cthief), bal(cvictim))
free_victim()

# штраф не больше свободного баланса вора
alter("UPDATE slot_accounts SET balance=1 WHERE user_id=?", (ct["id"],))
alter("UPDATE slot_accounts SET balance=1000 WHERE user_id=?", (cv["id"],))
r = steal(cthief, cv["id"], 206, 20)
assert r.status_code == 200, r.text
g = guess(cvictim, r.json()["id"], ct["id"], 207)
assert g.json()["caught"] is True and g.json()["compensation"] == 1, g.text
assert bal(cthief) == 0 and bal(cvictim) == 1001, (bal(cthief), bal(cvictim))
free_victim()

# amount=0 — просто отпетунить, жетоны не трогаются
r = steal(cthief, cv["id"], 208, 0)
assert r.status_code == 200 and r.json()["amount"] == 0 and bal(cvictim) == 1001, r.text
g = guess(cvictim, r.json()["id"], co["id"], 209)
assert g.json()["caught"] is False and bal(cthief) == 0, g.text
free_victim()                                                # жертва отпетунена: баланс смотрим после снятия блокировки
assert bal(cvictim) == 1001

# у жертвы почти нет жетонов — красть нечего, но отпетунить можно
free_victim()
alter("UPDATE slot_accounts SET balance=2 WHERE user_id=?", (cv["id"],))
r = steal(cthief, cv["id"], 210, 1)
assert r.status_code == 400 and "недостаточно" in r.json()["detail"], r.text
assert steal(cthief, cv["id"], 211, 0).status_code == 200

# =============================== отмена своей неразгаданной атаки ===============================
def cancel(client, attack_id):
    return client.post("/api/minigames/petun/cancel", json={"attack_id": attack_id})


my = cthief.get("/api/minigames/petun/state").json()["my_attack"]
assert my and cvictim.get("/api/minigames/petun/status").json()["pending_count"] == 1
assert cancel(cvictim, my["id"]).status_code == 404 and cancel(outsider, my["id"]).status_code == 404   # отменить может только нападающий
assert httpx.post(BASE + "/api/minigames/petun/cancel", json={"attack_id": my["id"]}).status_code == 401
r = cancel(cthief, my["id"])
assert r.status_code == 200 and r.json()["ok"], r.text
st = cthief.get("/api/minigames/petun/state").json()
assert st["has_active"] is False and st["my_attack"] is None
assert cvictim.get("/api/minigames/petun/status").json()["pending_count"] == 0
assert guess(cvictim, my["id"], ct["id"], 220).status_code == 409                    # отменённое отгадывать нельзя
assert cancel(cthief, my["id"]).status_code == 409                                    # повторная отмена
r = steal(cthief, cv["id"], 221, 0)
assert r.status_code == 429, "отмена не обходит «раз в 5 минут»: нельзя спамить «атака — отмена»"

# отмена возвращает замороженные жетоны жертве
free_victim()
alter("UPDATE slot_accounts SET balance=1000 WHERE user_id=?", (cv["id"],))
thief_before = bal(cthief)
r = steal(cthief, cv["id"], 222, 100)
assert r.status_code == 200 and bal(cvictim) == 900, r.text
r = cancel(cthief, r.json()["id"])
assert r.status_code == 200 and r.json()["returned"] == 100, r.text
assert bal(cvictim) == 1000 and bal(cthief) == thief_before, (bal(cvictim), bal(cthief))
assert cthief.get("/api/minigames/petun/state").json()["frozen"] == 0

for c in (cthief, cvictim, cother):
    c.close()
for c in (thief, victim, other, third, outsider):
    c.close()
print("OK: отпетунивание — друг, одна атака, раз в 5 минут, угадал/не угадал/не ответил, блокировка только на курсы, идемпотентность")
