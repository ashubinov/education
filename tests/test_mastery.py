"""Цикл «повторять до улучшения» на мок-ИИ.
Запуск: tests/restart_test_server.sh clean && LQ_DB=<tmp.db> LLM_MOCK=1 python tests/test_mastery.py http://127.0.0.1:8011"""
import glob
import json
import os
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)
from server import engine, generator  # noqa: E402
from server.db import db  # noqa: E402

c = httpx.Client(base_url=BASE, timeout=60)
tok = c.post("/api/auth/register", json={"username": f"mast{int(time.time()) % 100000}", "password": "secret1"}).json()["token"]
c.headers["Authorization"] = f"Bearer {tok}"
from tests.fixtures import sample_files  # noqa: E402
files = [("files", f) for f in sample_files()[:2]]
cid = c.post("/api/courses", files=files).json()["id"]


def nxt():
    for _ in range(200):
        n = c.get(f"/api/courses/{cid}/next").json()
        if n["state"] in ("ready", "completed", "failed", "error"):
            return n
        time.sleep(0.3)
    raise SystemExit("timeout next")


def solve(lid, wrong_share=0.0):
    """wrong_share — доля неверных ответов в уроке (0..1)."""
    v = c.post(f"/api/lessons/{lid}/start", json={}).json()
    rid = v["run_id"]
    graded = 0
    total = sum(1 for s in engine.build_steps(db.one("select * from lessons where id=?", (lid,))) if s["step"] != "read")
    n_wrong = round(total * wrong_share)
    while v["step"]:
        st, k = v["step"], v["step"]["kind"]
        lesson = db.one("select * from lessons where id=?", (lid,))
        state = json.loads(db.one("select * from runs where id=?", (rid,))["state"])
        ref = engine.all_steps(lesson, state)[st["idx"]]
        bad = k != "read" and not st.get("retry") and graded < n_wrong
        if k != "read" and not st.get("retry"):
            graded += 1
        p = {"idx": st["idx"]}
        if k in ("single", "image"):
            p["choice"] = (ref["answer"] + 1) % len(ref["options"]) if bad else ref["answer"]
        elif k == "multi":
            p["choice"] = ref["answer"]
        elif k == "fill":
            p["text"] = "ничего" if bad else ref["accept"][0]
        elif k == "write":
            p["text"] = "ничего" if bad else ref["reference"]
        elif k == "task":
            p["text"] = "Развёрнутое решение задачи: показываем связь понятий и приводим пример применения."
        res = c.post(f"/api/runs/{rid}/answer", json=p).json()
        if res.get("self_check"):
            res = c.post(f"/api/runs/{rid}/answer", json={"self": 1}).json()
        v = res["next"]
    s = c.post(f"/api/runs/{rid}/finish").json()["summary"]
    time.sleep(1.5)  # даём after_lesson вставить повторы
    return s


def lessons():
    return [(l["type"], l["title"][:40], l["status"]) for l in generator.ordered_lessons(cid)][:14]


def run_until(type_, wrong_share, label):
    while True:
        n = nxt()
        assert n["state"] == "ready", n
        t = n["type"]
        if t == type_ and wrong_share is not None:
            s = solve(n["lesson_id"], wrong_share)
            print(f"{label}: {n['title'][:45]} -> {s['score']:.2f}")
            return s, n
        solve(n["lesson_id"])


# 1) проверка терминов: первая попытка провалена -> вставлено «работа над ошибками» + повторная проверка
before = len([1 for _ in generator.ordered_lessons(cid)])
s1, n1 = run_until("terms_test", 0.7, "terms_test №1 (плохо)")
assert s1["score"] < 0.7
after = generator.ordered_lessons(cid)
nxt_two = [(l["type"], l["title"]) for l in after if l["idx"] is not None][:0]
ids = [l["id"] for l in after]
pos = ids.index(n1["lesson_id"])
assert after[pos + 1]["type"] == "terms" and "ошибками" in after[pos + 1]["title"], [(l["type"], l["title"]) for l in after[pos:pos + 3]]
assert after[pos + 2]["type"] == "terms_test", after[pos + 2]
print("OK: после провала вставлены", after[pos + 1]["title"][:50], "+", after[pos + 2]["title"])

# 2) повтор: теперь отвечаем хорошо -> цепочка заканчивается, новых вставок нет
n = nxt()
assert n["type"] == "terms" and n["lesson_id"] == after[pos + 1]["id"], n
solve(n["lesson_id"])
s2, n2 = run_until("terms_test", 0.0, "terms_test №2 (хорошо)")
assert s2["score"] >= 0.8
after2 = generator.ordered_lessons(cid)
ids2 = [l["id"] for l in after2]
assert after2[ids2.index(n2["lesson_id"]) + 1]["type"] != "terms" or "ошибками" not in after2[ids2.index(n2["lesson_id"]) + 1]["title"], "повтор не должен продолжаться после успеха"
print("OK: после успеха повторы прекращены")

# 3) «заметное улучшение»: 3 неудачные попытки подряд упираются в максимум повторов
hist = [0.3, 0.5, 0.55, 0.6]
assert generator.needs_more([0.3]) and generator.needs_more([0.3, 0.5]) and not generator.needs_more([0.3, 0.75])      # +0.45 и ≥0.7
assert not generator.needs_more([0.5, 0.82]) and not generator.needs_more(hist) and not generator.needs_more([0.75])
assert generator.needs_more([0.6, 0.65]) and not generator.needs_more([0.6, 0.8])
print("OK: правило остановки (хорошо / заметно лучше / лимит попыток)")

# 4) контрольный тест: провал -> работа над ошибками -> проверка -> повторный контрольный
while True:
    n = nxt()
    if n["type"] == "final_test":
        break
    solve(n["lesson_id"])
sf, nf = (solve(n["lesson_id"], 0.8), n)
print("final №1:", round(sf["score"], 2))
assert sf["score"] < 0.7
tail = [(l["type"], l["title"][:30]) for l in generator.ordered_lessons(cid)]
fi = [l["id"] for l in generator.ordered_lessons(cid)].index(nf["lesson_id"])
types = [t for t, _ in tail[fi + 1:fi + 4]]
assert types == ["terms", "terms_test", "final_test"], types
print("OK: после проваленного контрольного вставлены", types)
print("ALL OK")
