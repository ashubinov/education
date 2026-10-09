"""Резервные копии базы: создание, список, скачивание, целостность копии, ограничение числа копий, доступ только администратору.
Запуск (чистый сервер, BACKUP_KEEP=3): python tests/test_backup.py http://127.0.0.1:8011"""
import gzip
import os
import sqlite3
import sys
import tempfile
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
tag = str(int(time.time()) % 100000)


def reg(name):
    d = httpx.post(f"{BASE}/api/auth/register", json={"username": f"{name}{tag}", "password": "secret1"}, timeout=30).json()
    return httpx.Client(base_url=BASE, timeout=60, headers={"Authorization": f"Bearer {d['token']}"}), d["user"]


adm, au = reg("adm")
usr, uu = reg("usr")
assert au["is_admin"] and not uu["is_admin"]

assert usr.get("/api/admin/backups").status_code == 403
assert usr.post("/api/admin/backups").status_code == 403
assert usr.get("/api/admin/backups/learnquest-1.db.gz").status_code == 403

info = adm.post("/api/admin/backups").json()
assert info["name"].startswith("learnquest-") and info["name"].endswith(".db.gz") and info["size"] > 1000, info
lst = adm.get("/api/admin/backups").json()
assert [b["name"] for b in lst["backups"]] == [info["name"]] and lst["keep"] >= 1

# после копии появляется ещё один пользователь — в копии его быть не должно
late, lu = reg("late")
r = adm.get(f"/api/admin/backups/{info['name']}")
assert r.status_code == 200 and r.headers["content-type"].startswith("application/gzip")
raw = gzip.decompress(r.content)
assert raw.startswith(b"SQLite format 3"), "это не файл SQLite"
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    path = os.path.join(tmp, "copy.db")
    open(path, "wb").write(raw)
    con = sqlite3.connect(path)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    names = {x[0] for x in con.execute("SELECT username FROM users")}
    con.close()
assert au["username"] in names and uu["username"] in names and lu["username"] not in names, names
print("OK: копия создаётся, скачивается, проходит проверку целостности и содержит данные на момент снимка")

# защита имени файла и ограничение числа копий
for bad in ("..%2F..%2Fetc%2Fpasswd", "learnquest-1.db", "x.db.gz", "learnquest-abc.db.gz"):
    assert adm.get(f"/api/admin/backups/{bad}").status_code in (400, 404), bad
keep = adm.get("/api/admin/backups").json()["keep"]
for _ in range(keep + 2):
    adm.post("/api/admin/backups")
names = [b["name"] for b in adm.get("/api/admin/backups").json()["backups"]]
assert len(names) == keep and names == sorted(names, reverse=True), names
assert info["name"] not in names, "старые копии должны удаляться"
print(f"OK: хранятся только последние {keep} копий, чужие имена файлов не принимаются")
print("ALL OK")
