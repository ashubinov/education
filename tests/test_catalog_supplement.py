"""Дополнение готового курса каталога новым материалом: новые модули появляются у шаблона и у всех копий, прогресс не теряется.
Запуск: tests/restart_test_server.sh clean && LQ_DB=<tmp.db> LLM_MOCK=1 python tests/test_catalog_supplement.py http://127.0.0.1:8011"""
import json
import os
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)
from server.db import db  # noqa: E402

anon = httpx.Client(base_url=BASE, timeout=60)
u = f"sup{int(time.time()) % 100000}"
a = anon.post("/api/auth/register", json={"username": u, "password": "secret1"}).json()
admin = httpx.Client(base_url=BASE, timeout=60, headers={"Authorization": f"Bearer {a['token']}"})
assert a["user"]["is_admin"]
seed = os.path.join(root, "catalog_seed.json")
admin.post("/api/admin/catalog/import?replace=1", files=[("file", ("c.json", open(seed, "rb"), "application/json"))]).raise_for_status()
friend = anon.post("/api/auth/register", json={"username": u + "f", "password": "secret1"}).json()
fr = httpx.Client(base_url=BASE, timeout=60, headers={"Authorization": f"Bearer {friend['token']}"})


def synthetic(name="лекция5.txt", chars=1234):
    """Маленький пакет дополнения: 1 новый файл, 2 фрагмента, 1 модуль, 2 урока (термины + парный тест)."""
    plan = {"terms": [{"term": "Прерывание", "gist": "сигнал процессору"}], "practice_tasks": 0, "focus": "-"}
    terms = {"steps": [{"kind": "reveal", "title": "Прерывание"}]}
    return {"version": 1, "kind": "supplement", "title": "Тестовое дополнение", "has_practice": 0, "chunk_base": 100, "catalog_no": 102,
            "sources": [{"filename": name, "chars": chars, "text": "Прерывание — сигнал процессору."}],
            "chunks": [{"idx": 101, "text": "Прерывание — это сигнал.", "digest": None}, {"idx": 102, "text": "Обработчик прерывания.", "digest": None}],
            "modules": [{"title": "Прерывания", "summary": "Что такое прерывание", "objectives": json.dumps(["Назвать прерывание"]),
                         "chunk_ids": json.dumps([101, 102]), "practical": 0, "plan": json.dumps(plan), "kind": "main"}],
            "lessons": [{"id": 9001, "module_rel": 0, "idx": 0, "type": "terms", "title": "Термины: прерывание", "minutes": 4,
                         "content": json.dumps(terms), "meta": "{}"},
                        {"id": 9002, "module_rel": 0, "idx": 1, "type": "terms_test", "title": "Тест: прерывание", "minutes": 3,
                         "content": json.dumps({"questions": []}), "meta": json.dumps({"for": 9001})}]}


def upload(client, number, pkg):
    return client.post(f"/api/admin/catalog/{number}/supplement", files=[("file", ("s.json", json.dumps(pkg).encode("utf-8"), "application/json"))])


def upload_real_again(client, path):
    return client.post("/api/admin/catalog/102/supplement", files=[("file", ("r.json", open(path, "rb"), "application/json"))]).json()


def snapshot(cid):
    return {l["id"]: (l["status"], l["score"]) for l in db.q("SELECT id, status, score FROM lessons WHERE course_id=?", (cid,))}


# 1) у друга уже есть копия курса №102 с прогрессом; у админа — своя копия, у шаблона — исходные модули
mine = fr.post("/api/catalog/102/add").json()["course_id"]
adm_copy = admin.post("/api/catalog/102/add").json()["course_id"]
tmpl = db.one("SELECT id FROM courses WHERE is_template=1 AND catalog_no=102")["id"]
first = db.one("SELECT id FROM lessons WHERE course_id=? ORDER BY module_id, idx LIMIT 1", (mine,))["id"]
db.x("UPDATE lessons SET status='done', score=0.9, completed_at=CURRENT_TIMESTAMP WHERE id=?", (first,))
db.x("UPDATE courses SET status='completed' WHERE id=?", (mine,))  # курс «пройден»: после дополнения должен снова стать активным
before = snapshot(mine)
n_mod0 = db.val("SELECT COUNT(*) FROM modules WHERE course_id=?", (mine,))
n_les0 = db.val("SELECT COUNT(*) FROM lessons WHERE course_id=?", (mine,))
max_chunk0 = db.val("SELECT MAX(idx) FROM chunks WHERE course_id=?", (mine,))
assert before[first] == ("done", 0.9)

# 2) права и некорректные файлы
pkg = synthetic()
assert upload(fr, 102, pkg).status_code == 403
assert upload(admin, 999, pkg).status_code == 404
assert admin.post("/api/admin/catalog/102/supplement", files=[("file", ("x.json", b"not json", "application/json"))]).status_code == 400
assert upload(admin, 102, {"kind": "course", "modules": [], "lessons": []}).status_code == 400
assert snapshot(mine) == before
print("OK: загрузить дополнение может только админ; чужие и битые файлы отклоняются без изменений")

# 3) применение: шаблон и обе копии получили новый модуль и уроки, старый прогресс не тронут
r = upload(admin, 102, pkg)
assert r.status_code == 200, r.text
res = r.json()
assert res["template"] == "added" and res["copies_added"] == 2 and res["copies_skipped"] == 0, res
for cid in (tmpl, mine, adm_copy):
    assert db.val("SELECT COUNT(*) FROM modules WHERE course_id=?", (cid,)) == n_mod0 + 1, cid
    assert db.val("SELECT COUNT(*) FROM lessons WHERE course_id=?", (cid,)) == n_les0 + 2, cid
after = snapshot(mine)
assert all(after[i] == v for i, v in before.items()), "прогресс по старым урокам изменился"
assert after[first] == ("done", 0.9)
new = [i for i in after if i not in before]
assert len(new) == 2 and all(after[i][0] == "ready" for i in new)
m = db.one("SELECT * FROM modules WHERE course_id=? ORDER BY idx DESC LIMIT 1", (mine,))
assert m["title"] == "Прерывания" and m["idx"] == n_mod0 and m["status"] == "planned"
assert json.loads(m["chunk_ids"]) == [max_chunk0 + 1, max_chunk0 + 2], m["chunk_ids"]
assert sorted(c["idx"] for c in db.q("SELECT idx FROM chunks WHERE course_id=?", (mine,)))[-2:] == [max_chunk0 + 1, max_chunk0 + 2]
terms_id = db.one("SELECT id FROM lessons WHERE module_id=? AND type='terms'", (m["id"],))["id"]
paired = db.one("SELECT meta FROM lessons WHERE module_id=? AND type='terms_test'", (m["id"],))
assert json.loads(paired["meta"])["for"] == terms_id, "ссылка парного теста должна указывать на новый урок"
assert db.val("SELECT status FROM courses WHERE id=?", (mine,)) == "ready"
print("OK: новые модули добавлены шаблону и всем копиям, прежний прогресс сохранён, курс снова «в процессе»")

# 4) повторная загрузка того же файла ничего не меняет
res = upload(admin, 102, pkg).json()
assert res["template"] == "skipped" and res["copies_added"] == 0 and res["copies_skipped"] == 2, res
assert db.val("SELECT COUNT(*) FROM modules WHERE course_id=?", (mine,)) == n_mod0 + 1
assert snapshot(mine) == after
print("OK: повторная загрузка пропущена")

# 5) пользователь, который добавит курс после дополнения, получает его целиком, а второе дополнение (другой файл) ложится следом
late = anon.post("/api/auth/register", json={"username": u + "l", "password": "secret1"}).json()
lt = httpx.Client(base_url=BASE, timeout=60, headers={"Authorization": f"Bearer {late['token']}"})
lc = lt.post("/api/catalog/102/add").json()["course_id"]
assert db.val("SELECT COUNT(*) FROM modules WHERE course_id=?", (lc,)) == n_mod0 + 1
pkg2 = synthetic("лекция6.txt", 999)
pkg2["chunk_base"] = 200
pkg2["chunks"] = [{"idx": 201, "text": "Ещё один фрагмент.", "digest": None}]
pkg2["modules"][0]["title"] = "Ещё одна тема"
pkg2["modules"][0]["chunk_ids"] = json.dumps([201])
res = upload(admin, 102, pkg2).json()
assert res["copies_added"] == 3, res
last = db.q("SELECT title, idx FROM modules WHERE course_id=? ORDER BY idx", (mine,))
assert [x["title"] for x in last][-2:] == ["Прерывания", "Ещё одна тема"] and last[-1]["idx"] == n_mod0 + 1
assert snapshot(mine).items() >= after.items()
print("OK: поздние копии получают дополнение сразу, следующее дополнение добавляется после предыдущего")

# 6) копии, потерявшие связь с каталогом (после «Импорта с заменой» origin_id указывает на удалённый шаблон; у личного курса админа его нет),
#    находятся по составу материалов, получают дополнение и снова привязываются к шаблону
db.x("UPDATE courses SET origin_id=NULL WHERE id=?", (lc,))
db.x("UPDATE courses SET origin_id=987654 WHERE id=?", (mine,))
before_mine = snapshot(mine)
pkg3 = synthetic("лекция7.txt", 777)
pkg3["chunk_base"] = 300
pkg3["chunks"] = [{"idx": 301, "text": "Третий фрагмент.", "digest": None}]
pkg3["modules"][0]["title"] = "Третья тема"
pkg3["modules"][0]["chunk_ids"] = json.dumps([301])
res = upload(admin, 102, pkg3).json()
assert res["relinked"] == 2 and res["copies_added"] == 3, res
for cid in (lc, mine):
    assert db.val("SELECT origin_id FROM courses WHERE id=?", (cid,)) == tmpl
    assert db.one("SELECT 1 AS x FROM modules WHERE course_id=? AND title='Третья тема'", (cid,)), cid
assert snapshot(mine).items() >= before_mine.items()
# чужой курс с другими материалами не затрагивается
other = admin.post("/api/courses", files=[("files", ("o.txt", ("Совсем другая тема про сети. " * 30).encode(), "text/plain"))], data={"title": "Чужой"}).json()["id"]
for _ in range(100):
    if admin.get(f"/api/courses/{other}").json()["status"] == "ready":
        break
    time.sleep(0.3)
n_other = db.val("SELECT COUNT(*) FROM modules WHERE course_id=?", (other,))
pkg4 = synthetic("лекция8.txt", 888)
pkg4["chunk_base"] = 400
pkg4["chunks"] = [{"idx": 401, "text": "Четвёртый.", "digest": None}]
pkg4["modules"][0]["chunk_ids"] = json.dumps([401])
assert upload(admin, 102, pkg4).json()["relinked"] == 0
assert db.val("SELECT COUNT(*) FROM modules WHERE course_id=?", (other,)) == n_other
print("OK: потерявшие связь копии находятся по материалам и привязываются заново, посторонние курсы не трогаются")

# 7) настоящий файл дополнения (если подготовлен) применяется к копии и даёт модули с готовыми уроками
real = os.path.join(root, "catalog_supplement_102_4.json")
if os.path.exists(real):
    rp = json.load(open(real, encoding="utf-8"))
    res = admin.post("/api/admin/catalog/102/supplement", files=[("file", ("r.json", open(real, "rb"), "application/json"))])
    assert res.status_code == 200, res.text
    res = res.json()
    # если каталог (catalog_seed.json) уже содержит это дополнение, шаблон его пропускает, а копии, созданные из него, уже его имеют
    in_seed = res["template"] == "skipped"
    fname = rp["sources"][0]["filename"]
    for cid in (tmpl, mine, adm_copy, lc):
        assert db.one("SELECT 1 AS x FROM sources WHERE course_id=? AND filename=?", (cid, fname)), cid
        assert db.val("SELECT COUNT(*) FROM lessons WHERE course_id=? AND status NOT IN ('ready','done')", (cid,)) == 0, cid
    if not in_seed:
        assert res["copies_added"] == 3, res
        assert db.val("SELECT COUNT(*) FROM modules WHERE course_id=?", (mine,)) == n_mod0 + 2 + len(rp["modules"])
    assert upload_real_again(admin, real)["copies_added"] == 0
    print(f"OK: настоящее дополнение «{rp['title']}» ({'уже в каталоге' if in_seed else 'добавлено'}): {len(rp['modules'])} модулей, {len(rp['lessons'])} уроков")
print("ALL OK")
