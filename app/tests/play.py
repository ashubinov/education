"""Проходит N уроков курса от имени пользователя (для проверки интерфейса).
Использование: LQ_DB=<db> python tests/play.py <логин> <пароль> <course_id> <сколько_уроков> [ошибок_на_урок]
Верные ответы берутся из БД (серверу они не отдаются), поэтому нужен тот же LQ_DB, что и у сервера."""
import json
import os
import sys
import time

import httpx

sys.path.insert(0, ".")
from server import engine  # noqa: E402
from server.db import db  # noqa: E402

base = os.environ.get("PLAY_BASE", "http://127.0.0.1:8011")
user, pw, cid, n = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
mistakes = int(sys.argv[5]) if len(sys.argv) > 5 else 0
c = httpx.Client(base_url=base, timeout=90)
assert c.post("/api/auth/login", json={"username": user, "password": pw}).status_code == 200


def solve(lid, wrong):
    v = c.post(f"/api/lessons/{lid}/start", json={}).json()
    rid = v["run_id"]
    while v["step"]:
        st = v["step"]
        k = st["kind"]
        lesson = db.one("select * from lessons where id=?", (lid,))
        state = json.loads(db.one("select * from runs where id=?", (rid,))["state"])
        ref = engine.all_steps(lesson, state)[st["idx"]]
        p = {}
        bad = wrong > 0 and k != "read"
        if bad:
            wrong -= 1
        if k in ("single", "image"):
            p = {"choice": (ref["answer"] + 1) % len(ref["options"]) if bad else ref["answer"]}
        elif k == "multi":
            p = {"choice": [] if bad else ref["answer"]}
        elif k == "fill":
            p = {"text": "не знаю" if bad else ref["accept"][0]}
        elif k == "write":
            p = {"text": "не знаю" if bad else ref["reference"]}
        elif k == "task":
            p = {"text": "Развёрнутое решение: показываю связь понятий и привожу пример применения."}
        r = c.post(f"/api/runs/{rid}/answer", json=p).json()
        if r.get("self_check"):
            r = c.post(f"/api/runs/{rid}/answer", json={"self": 1}).json()
        v = r["next"]
    s = c.post(f"/api/runs/{rid}/finish").json()["summary"]
    return s


for i in range(n):
    for _ in range(120):
        nx = c.get(f"/api/courses/{cid}/next").json()
        if nx["state"] in ("ready", "completed", "failed", "error"):
            break
        time.sleep(0.5)
    if nx["state"] != "ready":
        print("stop:", nx)
        break
    s = solve(nx["lesson_id"], int(os.environ.get("MISTAKES_FINAL", mistakes)) if nx["type"] == "final_test" and not os.environ.get("_FAILED_ONCE") else mistakes)
    if nx["type"] == "final_test":
        os.environ["_FAILED_ONCE"] = "1"
    print(f"{i + 1}. {nx['type']:<13} {nx['title'][:40]:<40} score={s['score']:.2f} xp+{s['xp_total_gained']} lvl={s['level']['level']} streak={s['streak']['current']}")
