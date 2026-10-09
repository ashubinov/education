"""Проверка готового каталога: импорт catalog_seed.json, добавление курсов по номерам и полное прохождение всех уроков.
Запуск: tests/restart_test_server.sh clean && LQ_DB=<tmp.db> LLM_MOCK=1 python tests/verify_catalog.py http://127.0.0.1:8011
(нейросеть не нужна: уроки в каталоге сгенерированы заранее)"""
import json
import os
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)
from server import engine  # noqa: E402
from server.db import db  # noqa: E402

anon = httpx.Client(base_url=BASE, timeout=60)
u = f"cat{int(time.time()) % 100000}"
tok = anon.post("/api/auth/register", json={"username": u, "password": "secret1"}).json()["token"]
c = httpx.Client(base_url=BASE, timeout=60, headers={"Authorization": f"Bearer {tok}"})
seed = os.path.join(root, "catalog_seed.json")
r = c.post("/api/admin/catalog/import?replace=1", files=[("file", ("catalog_seed.json", open(seed, "rb"), "application/json"))])
print("импорт:", r.json())
cat = c.get("/api/catalog").json()
for x in cat:
    print(f"  №{x['number']} {x['title']}: модулей {x['modules']}, уроков {x['lessons']}, ≈{x['minutes']} мин")
assert [x["number"] for x in cat] == [101, 102, 103]
r2 = c.post("/api/admin/catalog/import", files=[("file", ("c.json", open(seed, "rb"), "application/json"))]).json()
assert r2["skipped"] == [101, 102, 103] and not r2["added"], r2


def solve(lid):
    v = c.post(f"/api/lessons/{lid}/start", json={}).json()
    rid = v["run_id"]
    n = 0
    while v["step"]:
        st, k = v["step"], v["step"]["kind"]
        lesson = db.one("select * from lessons where id=?", (lid,))
        state = json.loads(db.one("select * from runs where id=?", (rid,))["state"])
        ref = engine.all_steps(lesson, state)[st["idx"]]
        p = {"idx": st["idx"]}
        if k in ("single", "image"):
            p["choice"] = ref["answer"]
        elif k == "multi":
            p["choice"] = ref["answer"]
        elif k == "fill":
            p["text"] = ref["accept"][0]
        elif k == "write":
            p["text"] = ref["reference"]
        elif k == "task":
            p["text"] = "Развёрнутое решение задачи: показываем связь понятий и приводим пример применения подхода на практике."
        res = c.post(f"/api/runs/{rid}/answer", json=p).json()
        if res.get("self_check"):
            res = c.post(f"/api/runs/{rid}/answer", json={"self": 1}).json()
        v = res["next"]
        n += 1
    return c.post(f"/api/runs/{rid}/finish").json()["summary"], n


for number in (101, 102, 103):
    cid = c.post(f"/api/catalog/{number}/add").json()["course_id"]
    done, steps, images = 0, 0, 0
    kinds = {}
    while True:
        nx = c.get(f"/api/courses/{cid}/next").json()
        if nx["state"] != "ready":
            break
        lesson = db.one("select * from lessons where id=?", (nx["lesson_id"],))
        for s in engine.build_steps(lesson):
            kinds[s["step"]] = kinds.get(s["step"], 0) + 1
        s, n = solve(nx["lesson_id"])
        done += 1
        steps += n
        assert s["score"] >= 0.99 or nx["type"] == "practice", (nx["type"], nx["title"], s["score"])
    card = c.get(f"/api/courses/{cid}").json()
    print(f"№{number}: пройдено уроков {done}, шагов {steps}, итог: {nx['state']}, {card['progress']['percent']}%, виды шагов: {kinds}")
    assert nx["state"] == "completed" and card["progress"]["percent"] == 100, nx
print("ALL OK")
