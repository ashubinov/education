"""Клубничный вор (API, сквозной): запускается через tests/run_ci.py на одноразовой базе LQ_DB.

Проверяет: сумма кражи (1 … 30% баланса друга), заморозка добычи, штраф 10% пойманному вору (не больше его баланса),
таймаут 24 часа, идемпотентность, анонимность вора до ответа и системные сообщения в чате.
"""
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
TAG = str(time.time_ns() % 10**11)
DBPATH = os.environ.get("LQ_DB")


def reg(suffix):
    username = f"berry{suffix}{TAG}"
    res = httpx.post(BASE + "/api/auth/register", json={"username": username, "password": "secret1"}, timeout=30)
    assert res.status_code == 200, res.text
    data = res.json()
    return httpx.Client(base_url=BASE, headers={"Authorization": "Bearer " + data["token"]}, timeout=30), data["user"]


def friends(a, aa, b, bb):
    r = a.post("/api/friends/request", json={"username": bb["username"]})
    assert r.status_code == 200, r.text
    r = b.post(f"/api/friends/{aa['id']}/accept")
    assert r.status_code == 200, r.text


def balance(client):
    return client.get("/api/slots/state").json()["balance"]


def post_steal(client, victim_id, rid, amount=50):
    return client.post("/api/minigames/strawberry/steal", json={"victim_id": victim_id, "amount": amount, "request_id": rid})


def post_guess(client, tid, suspect, rid):
    return client.post("/api/minigames/strawberry/guess", json={"theft_id": tid, "suspect_id": suspect, "request_id": rid})


def alter(sql, args):
    assert DBPATH, "E2E test requires LQ_DB separate test database"
    with sqlite3.connect(DBPATH, timeout=30) as conn:
        conn.execute(sql, args)


def reset_cooldown(user):
    alter("UPDATE strawberry_thefts SET created_ts=created_ts-100 WHERE thief_id=?", (user["id"],))


thief, t = reg("thief")
victim, v = reg("victim")
other, o = reg("other")
outsider, u = reg("outsider")
friends(thief, t, victim, v)
friends(victim, v, other, o)
assert outsider.get("/api/minigames/strawberry/status").json()["pending_count"] == 0
assert post_steal(outsider, v["id"], "unauthorized-steal-01").status_code == 404
assert httpx.post(BASE + "/api/minigames/strawberry/steal", json={"victim_id": v["id"], "amount": 10, "request_id": "anonymous-user-0001"}).status_code == 401
assert post_steal(thief, t["id"], "same-user-00000001").status_code == 400
start_thief, start_victim = balance(thief), balance(victim)
assert start_victim == 1000
overview = victim.get("/api/minigames/strawberry/state").json()
assert overview["max_steal_percent"] == 30 and overview["catch_penalty_percent"] == 10 and overview["collateral"] == 0
assert thief.get("/api/minigames/strawberry/state").json()["friends"][0]["max_steal"] == 300

# Сумма проверяется на сервере: от 1 до 30% свободного баланса друга.
for bad in (0, -5, 301, 10**12):
    r = post_steal(thief, v["id"], "strawberry-bad-amount-01", bad)
    assert r.status_code in (400, 422), (bad, r.status_code, r.text)
assert balance(thief) == start_thief and balance(victim) == start_victim

# Кража не даёт вору свободных жетонов и ничего не стоит: добыча только заморожена.
r = post_steal(thief, v["id"], "strawberry-steal-0001", 100)
assert r.status_code == 200, r.text
s = r.json()
amount = s["amount"]
assert amount == 100 and s["reserved_fee"] == 0 and s["frozen"]
assert balance(thief) == start_thief
assert balance(victim) == start_victim - amount
assert thief.get("/api/minigames/strawberry/state").json()["frozen"] == amount
assert victim.get("/api/minigames/strawberry/status").json()["pending_count"] == 1
pending = victim.get("/api/minigames/strawberry/state").json()["pending"][0]
assert pending["id"] == s["id"] and set(x["id"] for x in pending["candidates"]) == {t["id"], o["id"]}
assert "thief_id" not in pending and "thief_username" not in pending
assert post_steal(thief, v["id"], "strawberry-steal-0002", 100).status_code == 409
assert post_steal(thief, v["id"], "strawberry-steal-0001", 100).json()["replayed"] is True
assert post_steal(thief, v["id"], "strawberry-steal-0001", 90).status_code == 409  # тот же request_id с другой суммой
assert balance(thief) == start_thief
chat = thief.get("/api/chat/messages").json()["messages"]
assert any("КЛУБНИЧАТ ПРЯМО СЕЙЧАС" in m["text"] and m["system_kind"] == "strawberry" for m in chat), "анонс начала кражи"
assert not any(t["username"].upper() in m["text"] for m in chat if m["system_kind"] == "strawberry"), "вор не раскрывается до ответа"

# Угадал: жертва получает украденное и штраф 10% с вора.
g = post_guess(victim, s["id"], t["id"], "guess-correct-00001")
assert g.status_code == 200, g.text
penalty = 10
assert g.json()["caught"] and g.json()["compensation"] == penalty and g.json()["amount"] == amount
assert balance(victim) == start_victim + penalty and balance(thief) == start_thief - penalty
assert post_guess(victim, s["id"], t["id"], "guess-correct-00001").json()["replayed"]
assert balance(victim) == start_victim + penalty and balance(thief) == start_thief - penalty
assert post_guess(victim, s["id"], o["id"], "guess-different-0001").status_code == 409
chat = thief.get("/api/chat/messages").json()["messages"]
assert any("СЛОВИЛ КРЕМПАЙ ОТ" in m["text"] and m["system_kind"] == "strawberry" for m in chat)

# Не угадал: добыча вору, жертве ничего не возвращается.
reset_cooldown(t)
r = post_steal(thief, v["id"], "strawberry-steal-0003", 50)
assert r.status_code == 200, r.text
s2 = r.json()
before_guess_thief = balance(thief)
before_guess_victim = balance(victim)
g = post_guess(victim, s2["id"], o["id"], "guess-wrong-000001")
assert g.status_code == 200, g.text
assert not g.json()["caught"] and g.json()["compensation"] == 0
assert balance(thief) == before_guess_thief + s2["amount"]
assert balance(victim) == before_guess_victim
chat = thief.get("/api/chat/messages").json()["messages"]
assert any("ОТКЛУБНИЧЕН ПО ПОЛНОЙ" in m["text"] and m["system_kind"] == "strawberry" for m in chat)

# 24 часа без ответа = вор выигрывает без участия жертвы.
reset_cooldown(t)
r = post_steal(thief, v["id"], "strawberry-steal-0004", 50)
assert r.status_code == 200, r.text
s3 = r.json()
before_timeout_thief = balance(thief)
alter("UPDATE strawberry_thefts SET expires_ts=? WHERE id=?", (int(time.time()) - 1, s3["id"]))
assert victim.get("/api/minigames/strawberry/status").json()["pending_count"] == 0
assert balance(thief) == before_timeout_thief + s3["amount"]
assert post_guess(victim, s3["id"], t["id"], "guess-too-late-0001").status_code == 409
bal = balance(thief)
victim.get("/api/minigames/strawberry/status")
assert balance(thief) == bal  # повторный расчёт ничего не меняет
assert thief.get("/api/minigames/strawberry/state").json()["frozen"] == 0

# Штраф не больше свободного баланса вора: у вора 3 жетона -> штраф 3, у вора 0 -> штрафа нет.
for have, expected in ((3, 3), (0, 0)):
    reset_cooldown(t)
    alter("UPDATE slot_accounts SET balance=? WHERE user_id=?", (have, t["id"]))
    r = post_steal(thief, v["id"], f"strawberry-poor-{have}-0001", 40)
    assert r.status_code == 200, r.text
    sp = r.json()
    assert balance(thief) == have, "кража не требует и не списывает жетоны вора"
    v_before = balance(victim)
    g = post_guess(victim, sp["id"], t["id"], f"guess-poor-{have}-000001")
    assert g.status_code == 200 and g.json()["caught"] and g.json()["compensation"] == expected, g.text
    assert balance(thief) == have - expected and balance(victim) == v_before + 40 + expected

# Одинаковые параллельные запросы — одна операция.
reset_cooldown(t)
before = balance(thief)
with ThreadPoolExecutor(max_workers=6) as ex:
    results = list(ex.map(lambda _: post_steal(thief, v["id"], "strawberry-race-0001", 20), range(6)))
assert all(r.status_code == 200 for r in results), [(r.status_code, r.text) for r in results]
assert len({r.json()["id"] for r in results}) == 1
assert balance(thief) == before

# Системные сообщения и обычные команды различаются.
msgs = other.get("/api/chat/messages").json()["messages"]
assert all(m.get("system_kind") is not None for m in msgs
           if "СЛОВИЛ КРЕМПАЙ" in m["text"] or "ОТКЛУБНИЧЕН ПО ПОЛНОЙ" in m["text"] or "КЛУБНИЧАТ" in m["text"])

for c in (thief, victim, other, outsider):
    c.close()
print("OK: клубничный вор — сумма до 30%, штраф 10% за поимку (не больше баланса вора), 24 часа, идемпотентность, анонимность до ответа")
