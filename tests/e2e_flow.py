"""Сквозной тест по HTTP (мок-ИИ): JWT, CORS, валидация, курс из файлов, уроки, цель, каталог, дубли, повтор, дополнение, экспорт/импорт.
Запуск: tests/restart_test_server.sh clean && LQ_DB=<tmp.db> LLM_MOCK=1 python tests/e2e_flow.py http://127.0.0.1:8011
Верные ответы берутся напрямую из БД (серверу они не отдаются)."""
import glob
import json
import os
import sqlite3
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
DB = os.environ.get("LQ_DB")
ALLOWED_ORIGIN = "https://ashubinov.github.io"
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)


def ok(r, code=None):
    if (code and r.status_code != code) or (not code and r.status_code >= 400):
        raise SystemExit(f"HTTP {r.status_code} {r.request.method} {r.request.url}: {r.text[:400]}")
    return r.json() if r.content else None


def wait(cond, what, timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = cond()
        if v:
            return v
        time.sleep(0.4)
    raise SystemExit(f"timeout: {what}")


def client(token=None):
    return httpx.Client(base_url=BASE, timeout=60, headers={"Authorization": f"Bearer {token}"} if token else {})


anon = client()

# ---------- 1. авторизация, JWT, валидация, CORS ----------
assert anon.get("/api/me").status_code == 401
r = anon.post("/api/auth/register", json={"username": "a b", "password": "123456"})
assert r.status_code == 422 and "username" in r.json()["detail"], r.text
r = anon.post("/api/auth/register", json={"username": "okname", "password": "123"})
assert r.status_code == 422, r.text
u = f"tester{int(time.time()) % 100000}"
s = ok(anon.post("/api/auth/register", json={"username": u, "password": "secret1", "display_name": "Тестер"}))
assert s["token"] and s["user"]["is_admin"], "первый пользователь — администратор"
admin = client(s["token"])
assert admin.get("/api/me").json()["username"] == u
assert client("bad.token.value").get("/api/me").status_code == 401
assert anon.post("/api/auth/login", json={"username": u, "password": "wrong"}).status_code == 400
assert ok(anon.post("/api/auth/login", json={"username": u, "password": "secret1"}))["token"]
assert admin.put("/api/me", json={"theme_color": "red"}).status_code == 422
assert admin.put("/api/me", json={"theme_color": "#10b981", "theme_mode": "light"}).json()["theme_color"] == "#10b981"
pre = anon.options("/api/courses", headers={"Origin": ALLOWED_ORIGIN, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"})
assert pre.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN, pre.headers
bad = anon.options("/api/courses", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
assert "access-control-allow-origin" not in bad.headers, bad.headers
assert "authorization" in pre.headers.get("access-control-allow-headers", "").lower()
print("OK: JWT, валидация, CORS")

# ---------- 2. курс из файлов и прохождение ----------
files = [("files", (os.path.basename(p), open(p, "rb"), "application/pdf")) for p in sorted(glob.glob(os.path.join(root, "test_files", "*.pdf")))]
r = ok(admin.post("/api/courses", files=files, data={"title": ""}))
cid = r["id"]
card = wait(lambda: (lambda d: d if d["status"] in ("ready", "error") else None)(ok(admin.get(f"/api/courses/{cid}"))), "course build")
assert card["status"] == "ready", card
print("курс:", card["title"], "| модулей:", len(card["modules"]))
con = sqlite3.connect(DB)


def solve_lesson(cl, lid, wrong=0):
    from server import engine
    from server.db import db
    v = ok(cl.post(f"/api/lessons/{lid}/start", json={}))
    rid = v["run_id"]
    while v["step"]:
        st, k = v["step"], v["step"]["kind"]
        lesson = db.one("select * from lessons where id=?", (lid,))
        state = json.loads(db.one("select * from runs where id=?", (rid,))["state"])
        ref = engine.all_steps(lesson, state)[st["idx"]]
        p = {}
        bad = wrong > 0 and k != "read"
        wrong -= 1 if bad else 0
        if k in ("single", "image"):
            p = {"choice": (ref["answer"] + 1) % len(ref["options"]) if bad else ref["answer"]}
        elif k == "multi":
            p = {"choice": ref["answer"]}
        elif k == "fill":
            p = {"text": "ничего" if bad else ref["accept"][0]}
        elif k == "write":
            p = {"text": "ничего" if bad else ref["reference"]}
        elif k == "task":
            p = {"text": "Развёрнутое решение задачи: показываем связь понятий и приводим пример применения."}
        p["idx"] = st["idx"]
        res = ok(cl.post(f"/api/runs/{rid}/answer", json=p))
        if res.get("self_check"):
            res = ok(cl.post(f"/api/runs/{rid}/answer", json={"self": 1}))
        v = res["next"]
    return ok(cl.post(f"/api/runs/{rid}/finish"))["summary"]


def play(cl, course_id, limit=200):
    n = 0
    while n < limit:
        nx = wait(lambda: (lambda d: d if d["state"] in ("ready", "completed", "failed", "error") else None)(ok(cl.get(f"/api/courses/{course_id}/next"))), "next")
        if nx["state"] != "ready":
            return nx["state"], n
        solve_lesson(cl, nx["lesson_id"])
        n += 1
    return "limit", n


# защита от «двойного клика»: ответ на уже пройденный шаг отклоняется
nx = wait(lambda: (lambda d: d if d["state"] == "ready" else None)(ok(admin.get(f"/api/courses/{cid}/next"))), "first lesson")
v = ok(admin.post(f"/api/lessons/{nx['lesson_id']}/start", json={}))
r1 = admin.post(f"/api/runs/{v['run_id']}/answer", json={"choice": 0, "idx": 0})
r2 = admin.post(f"/api/runs/{v['run_id']}/answer", json={"choice": 0, "idx": 0})
assert r1.status_code == 200 and r2.status_code == 400, (r1.status_code, r2.status_code)

state, n = play(admin, cid)
card = ok(admin.get(f"/api/courses/{cid}"))
print(f"пройдено уроков: {n + 1}, статус: {state}/{card['status']}, прогресс {card['progress']['percent']}%, XP {card['xp']}")
assert card["status"] == "completed" and card["progress"]["percent"] == 100
goal = ok(admin.put(f"/api/courses/{cid}/goal", json={"date": time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400 * 7))}))
assert goal["date"]
assert admin.put(f"/api/courses/{cid}/goal", json={"date": "2020-01-01"}).status_code == 400
st = ok(admin.get("/api/stats"))
assert st["totals"]["lessons"] >= n and st["heatmap"]["days"]
print("OK: курс, уроки, цель, статистика")

# ---------- 3. повтор курса ----------
ok(admin.post(f"/api/courses/{cid}/restart"))
card = ok(admin.get(f"/api/courses/{cid}"))
assert card["status"] == "ready" and card["progress"]["lessons_done"] == 0 and card["xp"] > 0, card["progress"]
assert wait(lambda: ok(admin.get(f"/api/courses/{cid}/next"))["state"] == "ready", "restart next")
print("OK: «пройти заново» (прогресс сброшен, XP остался)")

# ---------- 4. дополнение курса ----------
extra = ("Новая тема. Рекуррентная сеть — модель, обрабатывающая последовательность по шагам. " * 20).encode()
ok(admin.post(f"/api/courses/{cid}/supplement", files=[("files", ("extra.txt", extra, "text/plain"))]))
card = wait(lambda: (lambda d: d if d["status"] == "ready" else None)(ok(admin.get(f"/api/courses/{cid}"))), "supplement", 60)
assert len(card["modules"]) > 4 and card["sources"][-1]["filename"] == "extra.txt", card["modules"]
print("OK: дополнение курса, модулей теперь", len(card["modules"]))

# ---------- 5. каталог ----------
num = ok(admin.post(f"/api/courses/{cid}/publish", json={}))["number"]
assert num == 101, num
friend_s = ok(anon.post("/api/auth/register", json={"username": u + "f", "password": "secret1"}))
assert not friend_s["user"]["is_admin"]
friend = client(friend_s["token"])
assert friend.post(f"/api/courses/{cid}/publish").status_code in (403, 404)
assert friend.get("/api/admin/catalog/export").status_code == 403
cat = ok(friend.get("/api/catalog"))
assert cat[0]["number"] == 101 and not cat[0]["added"]
assert friend.get("/api/catalog/999").status_code == 404
add1 = ok(friend.post("/api/catalog/101/add"))
add2 = ok(friend.post("/api/catalog/101/add"))
assert add1["already"] is False and add2["already"] is True and add1["course_id"] == add2["course_id"], (add1, add2)
mine = ok(friend.get("/api/courses"))
assert len(mine) == 1 and mine[0]["catalog_no"] == 101 and mine[0]["progress"]["lessons_done"] == 0
llm_before = con.execute("select count(*) from lessons where content is not null").fetchone()[0]
# у друга уроки уже сгенерированы (копия) и его прогресс не влияет на чужой
nx = ok(friend.get(f"/api/courses/{add1['course_id']}/next"))
assert nx["state"] == "ready", nx
solve_lesson(friend, nx["lesson_id"])
assert ok(admin.get(f"/api/courses/{cid}"))["progress"]["lessons_done"] == 0
assert ok(friend.get("/api/catalog"))[0]["added"] is True
assert friend.get(f"/api/courses/{cid}").status_code == 404  # чужой курс недоступен
print("OK: каталог, поиск по номеру, защита от дублей, изоляция прогресса")

# ---------- 6. экспорт / импорт каталога ----------
exp = admin.get("/api/admin/catalog/export")
data = exp.json()
assert data["courses"][0]["catalog_no"] == 101 and data["courses"][0]["lessons"]
res = ok(admin.post("/api/admin/catalog/import", files=[("file", ("catalog_seed.json", exp.content, "application/json"))]))
assert res["skipped"] == [101] and not res["added"], res
data["courses"][0]["catalog_no"] = 777
res = ok(admin.post("/api/admin/catalog/import", files=[("file", ("c.json", json.dumps(data).encode(), "application/json"))]))
assert res["added"] == [777], res
assert admin.post("/api/admin/catalog/import", files=[("file", ("c.json", b"not json", "application/json"))]).status_code == 400
print("OK: экспорт/импорт каталога")

# ---------- 7. отзыв токенов ----------
ok(friend.post("/api/auth/logout-all"))
assert friend.get("/api/me").status_code == 401
assert ok(anon.post("/api/auth/login", json={"username": u + "f", "password": "secret1"}))["token"]
print("OK: logout-all отзывает токены")
print("ALL OK")
