"""API e2e: мемный автомат, общий баланс, separate histories, idempotency.
Запуск: python tests/test_slots_meme.py http://127.0.0.1:8011
Требует запущенный тестовый сервер и доступ к регистрации.
"""
import secrets
import sys
from concurrent.futures import ThreadPoolExecutor

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
TOKEN = secrets.token_hex(5)


def reg(prefix):
    r = httpx.post(BASE + "/api/auth/register", json={"username": prefix + TOKEN, "password": "secret1"}, timeout=30)
    assert r.status_code == 200, r.text
    j = r.json()
    return httpx.Client(base_url=BASE, headers={"Authorization": "Bearer " + j["token"]}, timeout=30), j["user"]


assert httpx.get(BASE + "/api/slots/meme/state").status_code == 401
assert httpx.post(BASE + "/api/slots/meme/spin", json={"bet": 20, "request_id": "anonymous0000001"}).status_code == 401
admin, _ = reg("mema")  # первый пользователь тестового сервера может стать админом
user, u = reg("memb")
other, ou = reg("memc")

dog_before = user.get("/api/slots/state").json()
state = user.get("/api/slots/meme/state").json()
assert state["machine_id"] == "meme", state
assert state["balance"] == dog_before["balance"] == 1000
assert state["history"] == []
assert state["meta"]["machine_id"] == "meme"
assert {s["id"] for s in state["meta"]["symbols"]} == {
    "rage", "hype", "shocked", "smile", "suspicious", "quiet", "facepalm", "risky", "laugh", "wild", "scatter", "bonus"
}
assert all(len(s["pays"]) == 3 for s in state["meta"]["symbols"] if s.get("pays"))
assert len(state["meta"]["lines"]) == 10
assert state["meta"]["payout_scale"] == 0.75 and state["meta"]["boost_chance"] == 0.25
for bad in [0, 3, 11, 15, 25, 100001]:
    r = user.post("/api/slots/meme/spin", json={"bet": bad, "request_id": "bad-request-000001"})
    assert r.status_code in (400, 422), (bad, r.text)

# одновременные идентичные запросы: сервер не списывает ставку дважды
request = {"bet": 20, "request_id": "concurrent-meme-0001"}
def call(_):
    with httpx.Client(base_url=BASE, headers=user.headers, timeout=30) as client:
        return client.post("/api/slots/meme/spin", json=request)
with ThreadPoolExecutor(max_workers=7) as ex:
    results = list(ex.map(call, range(7)))
assert all(r.status_code == 200 for r in results), [r.text for r in results]
views = [r.json() for r in results]
assert len({r["id"] for r in views}) == 1
assert sum(not r["replayed"] for r in views) == 1
spin = views[0]
assert spin["machine_id"] == "meme", spin
assert len(spin["reels"]) == 5 and all(len(col) == 3 for col in spin["reels"])
assert spin["net"] == spin["payout"] - spin["bet"]
assert spin["payout"] == sum(x["pay"] for x in spin["lines"]) + (spin["scatter"]["pay"] if spin["scatter"] else 0) + (spin["bonus"]["prize"] if spin["bonus"] else 0)
assert spin["tier"] in ("none", "small", "big", "mega", "jackpot")
assert spin["balance"] == state["balance"] - 20 + spin["payout"]
assert user.post("/api/slots/meme/spin", json={"bet": 30, "request_id": request["request_id"]}).status_code == 409

m = user.get("/api/slots/meme/state").json()
d = user.get("/api/slots/state").json()
assert m["balance"] == d["balance"] == spin["balance"]
assert m["stats"]["spins"] == 1 and len(m["history"]) == 1
assert d["history"] == [], "Мемные вращения не должны появляться как собаки"
assert d["stats"]["spins"] == 1, "Общая статистика по обоим автоматам"
assert other.get("/api/slots/meme/state").json()["history"] == []
assert other.get("/api/slots/state").json()["balance"] == 1000

# бонус общий; нельзя взять второй бонус в другом автомате
bonus = user.post("/api/slots/daily").json()
assert bonus["balance"] == spin["balance"] + bonus["amount"]
assert user.get("/api/slots/meme/state").json()["balance"] == bonus["balance"]
assert not user.get("/api/slots/meme/state").json()["daily"]["available"]
assert user.post("/api/slots/daily").status_code == 400

print("OK: мемные слоты, общая экономика, история, валидация, гонки и повтор запросов")
