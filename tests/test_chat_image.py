"""Фото в общем чате (API, сквозной; запускается через tests/run_ci.py на чистом сервере).

Проверяет: отправку JPEG и PNG с подписью, видимость другим, безопасные заголовки, уменьшение больших фото и удаление метаданных/«хвоста»,
отказ для подделок, обрывов, огромных по размерам и тяжёлых файлов, лимиты, идемпотентность, доступ без входа и удаление фото вместе с сообщением.
"""
import struct
import sys
import time
import uuid

import httpx
import pymupdf

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
TAG = uuid.uuid4().hex[:8]


def pic(w, h, fmt="jpeg", alpha=False):
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h), alpha)
    pix.clear_with(200)
    return pix.tobytes("jpeg") if fmt == "jpeg" else pix.tobytes("png")


def size_of(data):
    p = pymupdf.Pixmap(data)
    return p.width, p.height


def reg(prefix):
    r = httpx.post(BASE + "/api/auth/register", json={"username": prefix + TAG, "password": "secret1"}, timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    return httpx.Client(base_url=BASE, headers={"Authorization": "Bearer " + d["token"]}, timeout=60), d["user"]


def rid():
    return uuid.uuid4().hex


def send(c, data, caption="", request_id=None, name="a.jpg"):
    return c.post("/api/chat/image", files={"file": (name, data, "image/jpeg")}, data={"request_id": request_id or rid(), "caption": caption})


admin, au = reg("pica")      # первый пользователь — администратор
bob, bu = reg("picb")
JPG, PNG = pic(64, 48), pic(40, 40, "png")

# 0) без входа нельзя
assert httpx.post(BASE + "/api/chat/image", files={"file": ("a.jpg", JPG, "image/jpeg")}, data={"request_id": rid()}).status_code == 401

# 1) JPEG с подписью: видят другие, картинка отдаётся как image/jpeg с безопасными заголовками
r = send(bob, JPG, caption="смотрите!")
assert r.status_code == 200, r.text
m = r.json()
assert m["image_url"] and m["image_url"].startswith("/api/chat/images/") and m["text"] == "смотрите!" and m["gif_url"] is None, m
hist = admin.get("/api/chat/messages").json()["messages"]
assert any(x["id"] == m["id"] and x["image_url"] == m["image_url"] for x in hist)
img = httpx.get(BASE + m["image_url"])
assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg" and size_of(img.content) == (64, 48)
assert img.headers.get("x-content-type-options") == "nosniff" and "default-src 'none'" in img.headers.get("content-security-policy", "")
assert "immutable" in img.headers.get("cache-control", "")
print("OK: фото отправлено, видно другим, отдаётся безопасными заголовками")

# 2) PNG остаётся PNG; большое фото уменьшается; «хвост» после картинки не сохраняется
time.sleep(2.1)
r = send(bob, PNG, name="a.png")
assert r.status_code == 200, r.text
p = httpx.get(BASE + r.json()["image_url"])
assert p.headers["content-type"] == "image/png" and p.content.startswith(b"\x89PNG")
time.sleep(2.1)
r = send(bob, pic(3200, 2400))
assert r.status_code == 200, r.text
assert max(size_of(httpx.get(BASE + r.json()["image_url"]).content)) == 1600
time.sleep(2.1)
r = send(bob, JPG + b"<html><script>alert(1)</script></html>")
assert r.status_code == 200, r.text
assert b"<script>" not in httpx.get(BASE + r.json()["image_url"]).content
print("OK: PNG остаётся PNG, большое фото уменьшено, хвост отброшен")

# 3) подделки, обрывы, огромные размеры и тяжёлые файлы отклоняются
huge_png = bytearray(pic(40, 40, "png"))
huge_png[16:24] = struct.pack(">II", 20000, 20000)              # заголовок врёт про размер: до декодирования не доходит
bad = {
    "gif": b"GIF89a" + b"\x00" * 40,
    "html": b"<html><body>hello</body></html>" * 3,
    "пусто": b"",
    "обрыв jpeg": JPG[:30],
    "обрыв png": PNG[:20],
    "размер 20000×20000": bytes(huge_png),
}
for name, data in bad.items():
    time.sleep(0.05)
    r = send(bob, data)
    assert r.status_code == 400, (name, r.status_code, r.text)
assert send(bob, b"\xff\xd8\xff" + b"\x00" * 9_000_000).status_code == 400     # тяжелее 8 МБ
assert send(bob, JPG, caption="x" * 201).status_code == 422
print("OK: подделки, обрывы, огромные файлы и длинные подписи отклоняются")

# 4) идемпотентность: тот же request_id — то же сообщение, а не дубль
time.sleep(2.1)
same = rid()
a = send(admin, JPG, request_id=same).json()
b = send(admin, JPG, request_id=same).json()
assert a["id"] == b["id"] and b["replayed"], (a, b)

# 5) лимиты: не чаще 1 сообщения в 2 секунды
carol, cu = reg("picc")
assert send(carol, JPG).status_code == 200
assert send(carol, JPG).status_code == 429
print("OK: идемпотентность и лимит частоты")

# 6) удаление сообщения администратором убирает и картинку
url = m["image_url"]
assert httpx.get(BASE + url).status_code == 200
assert bob.delete(f"/api/chat/messages/{m['id']}").status_code == 403
assert admin.delete(f"/api/chat/messages/{m['id']}").status_code == 200
assert httpx.get(BASE + url).status_code == 404
assert httpx.get(BASE + "/api/chat/images/not-a-real-key").status_code == 404
print("ALL OK")
