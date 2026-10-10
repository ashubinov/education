"""Integration test: general chat, auth, deduplication, XSS text, user isolation, account cleanup.
Run via tests/run_ci.py against a clean temporary test server.
"""
import os
import sqlite3
import sys
import time
import uuid

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"


def user(prefix):
    token = uuid.uuid4().hex[:8]
    resp = httpx.post(BASE + "/api/auth/register", json={
        "username": prefix + token,
        "password": "test-password-only",
        "display_name": prefix + " Tester"
    }, timeout=25)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return httpx.Client(base_url=BASE, headers={"Authorization": "Bearer " + body["token"]}, timeout=25), body["user"]["id"]


assert httpx.get(BASE + "/api/chat/messages").status_code == 401
assert httpx.get(BASE + "/api/chat/updates").status_code == 401
assert httpx.post(BASE + "/api/chat/messages", json={"text": "hello", "request_id": "n" * 16}).status_code == 401

alice, aid = user("chata")
bob, bid = user("chatb")
initial = alice.get("/api/chat/messages")
assert initial.status_code == 200, initial.text
assert initial.json()["messages"] == []

msg = '<script>alert("xss")</script> привет!'
request_id = "chat-request-00001"
posted = alice.post("/api/chat/messages", json={"text": msg, "request_id": request_id})
assert posted.status_code == 200, posted.text
first = posted.json()
assert first["text"] == msg and first["user_id"] == aid and first["replayed"] is False
repeat = alice.post("/api/chat/messages", json={"text": msg, "request_id": request_id})
assert repeat.status_code == 200 and repeat.json()["id"] == first["id"] and repeat.json()["replayed"] is True

seen = bob.get("/api/chat/messages").json()
assert len(seen["messages"]) == 1 and seen["messages"][0]["text"] == msg
assert alice.get("/api/chat/updates?after=0").json()["events"][0]["message"]["id"] == first["id"]
assert alice.get("/api/chat/updates?after=" + str(seen["cursor"])).json()["events"] == []
assert alice.get("/api/chat/messages?before_id=" + str(first["id"])).json()["messages"] == []

assert bob.post("/api/chat/messages", json={"text": " ", "request_id": "space-test-123456"}).status_code == 422
assert bob.post("/api/chat/messages", json={"text": "hi", "request_id": "x"}).status_code == 422
assert bob.post("/api/chat/messages", json={"text": "A" * 1001, "request_id": "long-test-1234567"}).status_code == 422

# Another user can publish immediately, while Alice's second message is throttled.
other = bob.post("/api/chat/messages", json={"text": "Привет, я Боб", "request_id": "bob-request-000001"})
assert other.status_code == 200, other.text
blocked = alice.post("/api/chat/messages", json={"text": "second", "request_id": "second-request-1234"})
assert blocked.status_code == 429, blocked.text
assert len(bob.get("/api/chat/messages").json()["messages"]) == 2

# Trigger cleanup when account is deleted. Deletion propagates through event journal,
# but there is no moderator tool and no user-message censorship mechanism.
db_path = os.environ.get("LQ_DB")
if db_path:
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM users WHERE id=?", (aid,))
    conn.commit()
    conn.close()
    remaining = bob.get("/api/chat/messages").json()["messages"]
    assert len(remaining) == 1 and remaining[0]["user_id"] == bid
    changes = bob.get("/api/chat/updates?after=0").json()["events"]
    assert any(e["kind"] == "deleted" and e["message_id"] == first["id"] for e in changes)

alice.close(); bob.close()
print("OK: общий чат, авторизация, общая лента, XSS как текст, идемпотентность, лимиты, очистка аккаунта")
