"""Каталог: запрет дубликатов, удаление админом, авто-удаление дубликата (как №103 после публикации копии ОС).
Запуск: tests/restart_test_server.sh clean && LQ_DB=<tmp.db> LLM_MOCK=1 python tests/test_catalog_admin.py http://127.0.0.1:8011"""
import os
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)
from server import catalog  # noqa: E402
from server.db import db  # noqa: E402

anon = httpx.Client(base_url=BASE, timeout=60)
u = f"adm{int(time.time()) % 100000}"
a = anon.post("/api/auth/register", json={"username": u, "password": "secret1"}).json()
admin = httpx.Client(base_url=BASE, timeout=60, headers={"Authorization": f"Bearer {a['token']}"})
assert a["user"]["is_admin"]
seed = os.path.join(root, "catalog_seed.json")
r = admin.post("/api/admin/catalog/import?replace=1", files=[("file", ("c.json", open(seed, "rb"), "application/json"))]).json()
assert r["added"] == [101, 102, 103] and not r["duplicates_removed"], r

# 1) админ берёт курс №102 к себе и пытается опубликовать копию -> отказ, ссылка на номер 102
cid = admin.post("/api/catalog/102/add").json()["course_id"]
rep = admin.post(f"/api/courses/{cid}/publish", json={})
assert rep.status_code == 400 and "102" in rep.json()["detail"], (rep.status_code, rep.text)
print("OK: копия курса из каталога не публикуется:", rep.json()["detail"])

# 2) собственный курс с теми же материалами тоже считается дубликатом
srcs = db.q("select filename, text from sources where course_id=?", (cid,))
files = [("files", (s["filename"] + ".txt", s["text"].encode("utf-8"), "text/plain")) for s in srcs]
own = admin.post("/api/courses", files=files, data={"title": "Моя копия ОС"}).json()["id"]
for _ in range(100):
    st = admin.get(f"/api/courses/{own}").json()["status"]
    if st in ("ready", "error"):
        break
    time.sleep(0.3)
assert st == "ready"
rep = admin.post(f"/api/courses/{own}/publish", json={})
print("   собственный курс с теми же файлами:", rep.status_code, rep.json())
assert rep.status_code == 400 and "уже есть в каталоге" in rep.json()["detail"]
print("OK: курс с теми же материалами не публикуется")

# 3) разный по содержанию курс публикуется и получает следующий номер; затем админ его удаляет
fresh = admin.post("/api/courses", files=[("files", ("n.txt", ("Совсем другая тема: сети и протоколы. " * 40).encode(), "text/plain"))], data={"title": "Сети"}).json()["id"]
for _ in range(100):
    if admin.get(f"/api/courses/{fresh}").json()["status"] == "ready":
        break
    time.sleep(0.3)
num = admin.post(f"/api/courses/{fresh}/publish", json={}).json()["number"]
assert num == 104, num
assert [c["number"] for c in admin.get("/api/catalog").json()] == [101, 102, 103, 104]
friend = anon.post("/api/auth/register", json={"username": u + "f", "password": "secret1"}).json()
fr = httpx.Client(base_url=BASE, timeout=60, headers={"Authorization": f"Bearer {friend['token']}"})
assert fr.delete("/api/admin/catalog/104").status_code == 403
assert admin.delete("/api/admin/catalog/999").status_code == 404
assert admin.delete("/api/admin/catalog/104").status_code == 200
assert [c["number"] for c in admin.get("/api/catalog").json()] == [101, 102, 103]
print("OK: админ удаляет курс из каталога, обычный пользователь не может")

# 4) авто-очистка: дубликат уже в базе (как №103 у пользователя) удаляется, остаётся меньший номер
tree = catalog.read_tree(db.one("select id from courses where is_template=1 and catalog_no=102")["id"])
catalog.write_tree(tree, catalog.system_user_id(), template=True, catalog_no=104)
assert [c["number"] for c in catalog.list_catalog(None)] == [101, 102, 103, 104]
removed = catalog.dedupe_templates()
assert removed == [104], removed
assert [c["number"] for c in catalog.list_catalog(None)] == [101, 102, 103]
print("OK: дубликат №104 удалён автоматически при проверке (остался №102)")
print("ALL OK")
