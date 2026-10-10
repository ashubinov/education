"""Модерация общего чата и команд: администратор удаляет сообщения, изменяет и удаляет команды; сообщения заблокированных скрываются, разблокировка возвращает доступ.
Запуск на чистом тестовом сервере (первый зарегистрированный — администратор): python tests/test_chat_moderation.py http://127.0.0.1:8011"""
import sys
import time
import uuid

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"


def reg(prefix):
    r = httpx.post(BASE + "/api/auth/register", json={"username": prefix + uuid.uuid4().hex[:6], "password": "secret1"}, timeout=30).json()
    return httpx.Client(base_url=BASE, headers={"Authorization": "Bearer " + r["token"]}, timeout=30), r["user"]


def say(c, text, rid):
    for _ in range(5):
        r = c.post("/api/chat/messages", json={"text": text, "request_id": rid})
        if r.status_code != 429:
            return r
        time.sleep(2.2)
    return r


adm, au = reg("modadm")
u1, uu1 = reg("modusr")
u2, uu2 = reg("modtwo")
assert au["is_admin"] and not uu1["is_admin"]

# ---------- удаление сообщений ----------
m1 = say(u1, "плохое сообщение", "mod-msg-000000001").json()
m2 = say(u2, "нормальное сообщение", "mod-msg-000000002").json()
cursor = adm.get("/api/chat/messages").json()["cursor"]
assert u1.delete(f"/api/chat/messages/{m1['id']}").status_code == 403, "обычный пользователь удалять не может"
assert u1.delete(f"/api/chat/messages/{m2['id']}").status_code == 403
assert httpx.delete(BASE + f"/api/chat/messages/{m1['id']}").status_code == 401
assert adm.delete("/api/chat/messages/999999").status_code == 404
assert adm.delete(f"/api/chat/messages/{m1['id']}").status_code == 200
ids = [m["id"] for m in u2.get("/api/chat/messages").json()["messages"]]
assert m1["id"] not in ids and m2["id"] in ids
ev = u2.get(f"/api/chat/updates?after={cursor}").json()["events"]
assert any(e["kind"] == "deleted" and e["message_id"] == m1["id"] for e in ev), "открытые окна чата получают событие об удалении"
assert adm.delete(f"/api/chat/messages/{m1['id']}").status_code == 404, "повторное удаление"
print("OK: администратор удаляет сообщения")

# ---------- блокировка: сообщения скрываются, разблокировка возвращает ----------
m3 = say(u1, "ещё одно от u1", "mod-msg-000000003").json()
cursor = adm.get("/api/chat/messages").json()["cursor"]
assert adm.post(f"/api/admin/users/{uu1['id']}/ban", json={"reason": "спам"}).status_code == 200
hist = [m["id"] for m in u2.get("/api/chat/messages").json()["messages"]]
assert m3["id"] not in hist and m2["id"] in hist, "сообщения заблокированного скрыты"
ev = u2.get(f"/api/chat/updates?after={cursor}").json()["events"]
assert any(e["kind"] == "deleted" and e["message_id"] == m3["id"] for e in ev), "у тех, кто в чате сейчас, они исчезают сразу"
assert u1.get("/api/chat/messages").status_code == 403, "заблокированный в чат не входит"
assert adm.post(f"/api/admin/users/{uu1['id']}/unban").status_code == 200
assert u1.get("/api/chat/messages").status_code == 200, "после разблокировки доступ вернулся"
assert m3["id"] in [m["id"] for m in u2.get("/api/chat/messages").json()["messages"]], "и его сообщения снова видны в истории"
print("OK: блокировка скрывает сообщения, разблокировка возвращает")

# ---------- команды: изменить и удалить ----------
payload = {"command": "/модтест", "description": "Тестовая команда", "action": "add_chips", "amount": 10, "cooldown_value": 1, "cooldown_unit": "hours"}
cid = adm.post("/api/chat/commands", json=payload).json()["id"]
other = adm.post("/api/chat/commands", json={**payload, "command": "/другая"}).json()["id"]
new = {**payload, "command": "/модтест2", "description": "Изменённая команда", "amount": 77, "cooldown_value": 5, "cooldown_unit": "minutes"}
assert u1.put(f"/api/chat/commands/{cid}", json=new).status_code == 403
assert u1.delete(f"/api/chat/commands/{cid}").status_code == 403
assert httpx.put(BASE + f"/api/chat/commands/{cid}", json=new).status_code == 401
assert adm.put("/api/chat/commands/999999", json=new).status_code == 404
assert adm.put(f"/api/chat/commands/{cid}", json={**new, "command": "/другая"}).status_code == 409, "имя занято другой командой"
assert adm.put(f"/api/chat/commands/{cid}", json={**new, "amount": 0}).status_code == 422
assert adm.put(f"/api/chat/commands/{cid}", json={**new, "action": "hack"}).status_code == 422
assert adm.put(f"/api/chat/commands/{cid}", json=new).status_code == 200
row = [c for c in u2.get("/api/chat/commands").json()["commands"] if c["id"] == cid][0]
assert row["command"] == "/модтест2" and row["amount"] == 77 and row["cooldown_seconds"] == 300 and row["description"] == "Изменённая команда"
assert adm.put(f"/api/chat/commands/{cid}", json={**new, "command": "/МОДТЕСТ2"}).status_code == 200, "та же команда с другим регистром — это не дубль"
used = u2.post("/api/chat/commands/use", json={"command": "/модтест2", "request_id": "mod-use-0000000001"})
assert used.status_code == 200 and used.json()["chips"] == 77, used.text
assert u2.post("/api/chat/commands/use", json={"command": "/модтест", "request_id": "mod-use-0000000002"}).status_code == 404, "старое имя больше не работает"
assert adm.delete(f"/api/chat/commands/{cid}").status_code == 200
assert u2.post("/api/chat/commands/use", json={"command": "/модтест2", "request_id": "mod-use-0000000003"}).status_code == 404
assert cid not in [c["id"] for c in adm.get("/api/chat/commands").json()["commands"]] and other in [c["id"] for c in adm.get("/api/chat/commands").json()["commands"]]
assert adm.delete(f"/api/chat/commands/{cid}").status_code == 404
print("OK: администратор изменяет и удаляет команды")
print("ALL OK")
