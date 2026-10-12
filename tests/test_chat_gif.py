"""GIF в общем чате (API, сквозной; запускается через tests/run_ci.py на чистом сервере).

Проверяет: отправку настоящего GIF, подпись, видимость другим, отдачу картинки (тип, nosniff, CSP), отбрасывание «хвоста» после GIF,
отказ для поддельных/повреждённых/слишком больших файлов, лимиты, идемпотентность по request_id, доступ без входа и удаление гифки вместе с сообщением.
"""
import struct
import sys
import time
import uuid

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
TAG = uuid.uuid4().hex[:8]

# минимальный корректный GIF 1×1 (один кадр)
GIF = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00"
       b",\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")


def reg(prefix):
    r = httpx.post(BASE + "/api/auth/register", json={"username": prefix + TAG, "password": "secret1"}, timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    return httpx.Client(base_url=BASE, headers={"Authorization": "Bearer " + d["token"]}, timeout=30), d["user"]


def rid():
    return uuid.uuid4().hex


def send(c, data, caption="", request_id=None, name="a.gif"):
    return c.post("/api/chat/gif", files={"file": (name, data, "image/gif")}, data={"request_id": request_id or rid(), "caption": caption})


admin, au = reg("gifa")      # первый пользователь — администратор
bob, bu = reg("gifb")

# 0) без входа нельзя
assert httpx.post(BASE + "/api/chat/gif", files={"file": ("a.gif", GIF, "image/gif")}, data={"request_id": rid()}).status_code == 401

# 1) обычная отправка с подписью; сообщение видят другие, картинка отдаётся как image/gif
r = send(bob, GIF, caption="смотрите!")
assert r.status_code == 200, r.text
m = r.json()
assert m["gif_url"] and m["gif_url"].startswith("/api/chat/gifs/") and m["text"] == "смотрите!", m
hist = admin.get("/api/chat/messages").json()["messages"]
assert any(x["id"] == m["id"] and x["gif_url"] == m["gif_url"] for x in hist)
img = httpx.get(BASE + m["gif_url"])
assert img.status_code == 200 and img.headers["content-type"] == "image/gif" and img.content == GIF
assert img.headers.get("x-content-type-options") == "nosniff" and "default-src 'none'" in img.headers.get("content-security-policy", "")
assert "immutable" in img.headers.get("cache-control", "")
print("OK: гифка отправлена, видна другим, картинка отдаётся безопасными заголовками")

# 2) «хвост» после завершающего блока отбрасывается (нельзя спрятать скрипт/страницу в конце файла)
time.sleep(2.1)
r = send(bob, GIF + b"<html><script>alert(1)</script></html>")
assert r.status_code == 200, r.text
assert httpx.get(BASE + r.json()["gif_url"]).content == GIF
print("OK: хвост после GIF отброшен")

# 3) поддельные и повреждённые файлы отклоняются
bad = {
    "png под видом gif": b"\x89PNG\r\n\x1a\n" + b"\x00" * 60,
    "html": b"<html><body>hello</body></html>" * 3,
    "обрыв": GIF[:-6],
    "нет завершающего байта": GIF[:-1],
    "ширина 0": GIF[:6] + struct.pack("<HH", 0, 1) + GIF[10:],
    "ширина 5000": GIF[:6] + struct.pack("<HH", 5000, 1) + GIF[10:],
    "мусорный блок": GIF[:13] + b"\x00\x00\x00\x00\x00\x00\x00\x00\x00" + b"\x99" + GIF[13:],
    "пусто": b"",
}
for name, data in bad.items():
    time.sleep(0.05)
    r = send(bob, data)
    assert r.status_code == 400, (name, r.status_code, r.text)
big = GIF[:-1] + b"\x21\xfe" + (b"\xff" + b"x" * 255) * 12000 + b"\x00;"      # корректная структура, но > 3 МБ
assert len(big) > 3_000_000
assert send(bob, big).status_code == 400
many = GIF[:6] + struct.pack("<HH", 2000, 2000) + GIF[10:-1] + GIF[GIF.index(b","):-1] * 20 + b";"   # 2000×2000 × 21 кадр > лимита «пикселей»
r = send(bob, many)
assert r.status_code == 400, r.text
assert send(bob, GIF, caption="x" * 201).status_code == 422
print("OK: подделки, обрывы, огромные файлы и длинные подписи отклоняются")

# 4) идемпотентность: тот же request_id — то же сообщение, а не дубль
time.sleep(2.1)
same = rid()
a = send(admin, GIF, request_id=same).json()
b = send(admin, GIF, request_id=same).json()
assert a["id"] == b["id"] and b["replayed"], (a, b)

# 5) лимиты: не чаще 1 сообщения в 2 секунды и не больше 5 гифок в минуту
carol, cu = reg("gifc")
assert send(carol, GIF).status_code == 200
assert send(carol, GIF).status_code == 429               # слишком быстро
print("OK: идемпотентность и лимиты частоты")

# 6) удаление сообщения администратором убирает и картинку
url = m["gif_url"]
assert httpx.get(BASE + url).status_code == 200
assert bob.delete(f"/api/chat/messages/{m['id']}").status_code == 403
assert admin.delete(f"/api/chat/messages/{m['id']}").status_code == 200
assert httpx.get(BASE + url).status_code == 404
assert httpx.get(BASE + "/api/chat/gifs/not-a-real-key").status_code == 404
print("ALL OK")
