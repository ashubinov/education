"""Интеграционный тест команд чата на отдельном тестовом сервере из tests/run_ci.py."""
from __future__ import annotations

import sys
import time
import uuid

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"


def register(prefix):
    name = prefix + uuid.uuid4().hex[:8]
    response = httpx.post(BASE + "/api/auth/register", json={
        "username": name, "password": "test-password-only"
    }, timeout=30)
    assert response.status_code == 200, response.text
    payload = response.json()
    return httpx.Client(base_url=BASE, timeout=35,
                        headers={"Authorization": "Bearer " + payload["token"]}), payload["user"]["id"]


assert httpx.get(BASE + "/api/chat/commands").status_code == 401
assert httpx.post(BASE + "/api/chat/commands/use", json={"command": "/bone", "request_id": "x" * 16}).status_code == 401
admin, aid = register("cmdadmin")
a, uid = register("cmda")
b, bid = register("cmdb")

# Пользователи не могут создавать/включать/выключать команды.
payload = {"command": "/kost", "description": "Получить виртуальные жетоны", "action": "add_chips",
           "amount": 73, "cooldown_value": 2, "cooldown_unit": "seconds"}
assert a.post("/api/chat/commands", json=payload).status_code == 403
assert admin.post("/api/chat/commands", json={**payload, "action": "arbitrary_api"}).status_code == 422
assert admin.post("/api/chat/commands", json={**payload, "amount": 100000000}).status_code == 422
assert admin.post("/api/chat/commands", json={**payload, "cooldown_value": 900, "cooldown_unit": "hours"}).status_code == 422

created = admin.post("/api/chat/commands", json=payload)
assert created.status_code == 200, created.text
cid = created.json()["id"]
assert admin.post("/api/chat/commands", json=payload).status_code == 409
lst = a.get("/api/chat/commands")
assert lst.status_code == 200
assert lst.json()["is_admin"] is False
row = next(x for x in lst.json()["commands"] if x["id"] == cid)
assert row["amount"] == 73 and row["cooldown_seconds"] == 2 and row["remaining_seconds"] == 0

before = a.get("/api/slots/state").json()["balance"]
b_before = b.get("/api/slots/state").json()["balance"]
request_id = "cmd-request-000001"
used = a.post("/api/chat/commands/use", json={"command": "/kost", "request_id": request_id})
assert used.status_code == 200, used.text
u = used.json()
assert u["chips"] == 73 and u["balance"] == before + 73 and u["replayed"] is False
assert a.get("/api/slots/state").json()["balance"] == before + 73
assert b.get("/api/slots/state").json()["balance"] == b_before, "чужой баланс не изменён"

# Идемпотентность: повтор одного запроса не начисляет дважды, даже когда таймаут ещё идёт.
rep = a.post("/api/chat/commands/use", json={"command": "/kost", "request_id": request_id})
assert rep.status_code == 200 and rep.json()["replayed"] is True
assert rep.json()["balance"] == before + 73
assert a.get("/api/slots/state").json()["balance"] == before + 73
assert a.post("/api/chat/commands/use", json={"command": "/kost", "request_id": "cmd-request-000002"}).status_code == 429

# Другому игроку та же команда доступна независимо от таймаута первого.
other = b.post("/api/chat/commands/use", json={"command": "/kost", "request_id": "cmd-other-000001"})
assert other.status_code == 200 and other.json()["balance"] == b_before + 73
assert a.get("/api/chat/commands").json()["commands"][0]["remaining_seconds"] > 0

# Вызов создаёт общее событие чата.
messages = admin.get("/api/chat/messages").json()["messages"]
assert any(m["id"] == u["message_id"] and "/kost" in m["text"] for m in messages)

# Время охлаждения действительно истекает, начисление происходит повторно.
time.sleep(2.2)
second = a.post("/api/chat/commands/use", json={"command": "/kost", "request_id": "cmd-request-000003"})
assert second.status_code == 200 and second.json()["balance"] == before + 146

# Выключение команды не стирает историю, но запрещает новые активации.
assert a.post(f"/api/chat/commands/{cid}/enabled?enabled=false").status_code == 403
changed = admin.post(f"/api/chat/commands/{cid}/enabled?enabled=false")
assert changed.status_code == 200
assert not any(c["id"] == cid for c in b.get("/api/chat/commands").json()["commands"])
assert b.post("/api/chat/commands/use", json={"command": "/kost", "request_id": "cmd-other-000002"}).status_code == 404
again = a.post("/api/chat/commands/use", json={"command": "/kost", "request_id": request_id})
assert again.status_code == 200 and again.json()["replayed"] is True

# Повторное включение сохраняет таймауты и ранее полученные начисления.
assert admin.post(f"/api/chat/commands/{cid}/enabled?enabled=true").status_code == 200
assert any(c["id"] == cid for c in a.get("/api/chat/commands").json()["commands"])

# Проверка единиц времени: минуты, часы.
for text, value, unit, seconds in (("/minute", 3, "minutes", 180), ("/hour", 2, "hours", 7200)):
    r = admin.post("/api/chat/commands", json={**payload, "command": text,
                   "cooldown_value": value, "cooldown_unit": unit})
    assert r.status_code == 200, r.text
    rows = a.get("/api/chat/commands").json()["commands"]
    assert next(c for c in rows if c["command"] == text)["cooldown_seconds"] == seconds

admin.close(); a.close(); b.close()
print("OK: создание команд админом, количество, кулдауны, изоляция, идемпотентность, чат, выключение")
