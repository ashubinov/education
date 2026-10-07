"""Сквозной тест по HTTP: регистрация -> курс из файлов -> прохождение уроков -> статистика.
Запуск: поднять сервер с LLM_MOCK=1 и LQ_DB=<tmp> и выполнить `python tests/e2e_flow.py http://127.0.0.1:8011`.
Верные ответы берутся напрямую из БД (серверу они не отдаются)."""
import json
import os
import sqlite3
import sys
import time
import glob

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
DB = os.environ.get("LQ_DB")
c = httpx.Client(base_url=BASE, timeout=60)


def ok(r):
    if r.status_code >= 400:
        raise SystemExit(f"HTTP {r.status_code} {r.request.url}: {r.text[:400]}")
    return r.json()


def wait(cond, what, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = cond()
        if v:
            return v
        time.sleep(0.5)
    raise SystemExit(f"timeout: {what}")


u = f"tester{int(time.time()) % 100000}"
me = ok(c.post("/api/auth/register", json={"username": u, "password": "secret1", "display_name": "Тестер"}))
print("registered", me["username"], "admin" if me["is_admin"] else "")

files = [("files", (os.path.basename(p), open(p, "rb"), "application/pdf")) for p in sorted(glob.glob("../test_files/*.pdf"))]
r = ok(c.post("/api/courses", files=files, data={"title": ""}))
cid = r["id"]
print("course", cid, r["warnings"])
card = wait(lambda: (lambda d: d if d["status"] in ("ready", "error") else None)(ok(c.get(f"/api/courses/{cid}"))), "course build", 90)
print("status", card["status"], card.get("error"), "| title:", card["title"], "| modules:", len(card["modules"]))
for m in card["modules"]:
    print("  -", m["title"], "|", m["state"])
assert card["status"] == "ready", card

con = sqlite3.connect(DB) if DB else None


def correct_payload(run_id, step):
    """Достаём верный ответ из БД (для теста)."""
    row = con.execute("select l.content, l.type, r.state from runs r join lessons l on l.id=r.lesson_id where r.id=?", (run_id,)).fetchone()
    content, typ, state = json.loads(row[0]), row[1], json.loads(row[2])
    return content, typ, state


def solve_lesson(lid, mistakes=0):
    v = ok(c.post(f"/api/lessons/{lid}/start", json={}))
    rid = v["run_id"]
    steps = 0
    wrong_budget = mistakes
    while v["step"]:
        st = v["step"]
        k = st["kind"]
        steps += 1
        payload = {}
        if k == "read":
            payload = {}
        elif k in ("single", "multi", "image", "fill", "write", "task"):
            # получаем эталон через внутренние шаги
            sys.path.insert(0, ".")
            from server import engine
            from server.db import db
            lesson = db.one("select * from lessons where id=?", (lid,))
            run = db.one("select * from runs where id=?", (rid,))
            state = json.loads(run["state"])
            ref = engine.all_steps(lesson, state)[st["idx"]]
            if k in ("single", "image"):
                payload = {"choice": ref["answer"] if wrong_budget <= 0 else (ref["answer"] + 1) % len(ref["options"])}
            elif k == "multi":
                payload = {"choice": ref["answer"]}
            elif k == "fill":
                payload = {"text": ref["accept"][0] if wrong_budget <= 0 else "ничего"}
            elif k == "write":
                payload = {"text": ref["reference"] if wrong_budget <= 0 else "ничего"}
            elif k == "task":
                payload = {"text": "Развёрнутое решение задачи: показываем связь понятий и приводим пример применения подхода на практике."}
            if wrong_budget > 0 and k != "read":
                wrong_budget -= 1
        res = ok(c.post(f"/api/runs/{rid}/answer", json=payload))
        v = res["next"]
        if res.get("self_check"):
            res = ok(c.post(f"/api/runs/{rid}/answer", json={"self": 1}))
            v = res["next"]
    fin = ok(c.post(f"/api/runs/{rid}/finish"))
    return steps, fin["summary"]


def next_ready():
    def f():
        n = ok(c.get(f"/api/courses/{cid}/next"))
        if n["state"] in ("ready", "completed", "failed", "error"):
            return n
        return None
    return wait(f, "next lesson", 90)


done = 0
while True:
    n = next_ready()
    if n["state"] != "ready":
        print("STOP:", n)
        break
    mistakes = 3 if n["type"] == "final_test" and done < 9 else 0
    steps, s = solve_lesson(n["lesson_id"], mistakes)
    done += 1
    print(f"lesson #{done:02d} {n['type']:<13} steps={steps:<3} score={s['score']:.2f} xp+{s['xp_total_gained']:<3} lvl={s['level']['level']} ach={[a['key'] for a in s['achievements']]}")
    if done > 80:
        break

card = ok(c.get(f"/api/courses/{cid}"))
print("final:", card["status"], card["progress"], "xp", card["xp"], "streak", card["streak"]["current"])
st = ok(c.get("/api/stats"))
print("stats totals:", st["totals"], "| week:", [d["xp"] for d in st["week"]], "| unlocked:", [a["key"] for a in st["achievements"] if a["unlocked"]])
ok(c.put(f"/api/courses/{cid}/goal", json={"date": time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400 * 7))}))
print("goal:", ok(c.get(f"/api/courses/{cid}"))["goal"])
print("OK")
