"""Свои темы: проверка ввода и автоисправление контраста, создание / правка / удаление, лимит тем, чужие темы недоступны,
выбор активной темы в профиле (и сброс при удалении), генерация по описанию (мок-ИИ) и её лимит, очистка при удалении пользователя.
Запуск (чистый сервер, LQ_DB — путь к его базе): LQ_DB=<tmp.db> python tests/test_themes.py http://127.0.0.1:8011"""
import os
import sqlite3
import sys
import time

import httpx

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
from server import themes as T  # noqa: E402

db_path = os.environ.get("LQ_DB")
SUFFIX = str(int(time.time()) % 100000)


def sql(q, args=()):
    con = sqlite3.connect(db_path, timeout=30)
    try:
        cur = con.execute(q, args)
        con.commit()
        return cur.fetchall()
    finally:
        con.close()


def reg(name):
    d = httpx.post(f"{BASE}/api/auth/register", json={"username": f"{name}{SUFFIX}", "password": "secret1"}, timeout=30).json()
    return httpx.Client(base_url=BASE, timeout=60, headers={"Authorization": f"Bearer {d['token']}"}), d["user"]


GOOD = {"name": "Ночной Токио", "colors": {"bg": "#0b0716", "card": "#150f2a", "ink": "#f1ecff", "accent": "#ff2e97", "ok": "#2cf5c9", "bad": "#ff5470"},
        "logo": "🌃", "decor": ["✨", "🌸"]}


def theme(**kw):
    return {**GOOD, **kw}


def assert_readable(t):
    c = t["colors"]
    assert T.contrast(c["ink"], c["bg"]) >= 4.5 and T.contrast(c["ink"], c["card"]) >= 4.5, ("текст нечитаем", c)
    assert T.contrast(c["accent"], c["bg"]) >= 3.0 and T.contrast(c["ok"], c["card"]) >= 3.0 and T.contrast(c["bad"], c["card"]) >= 3.0, ("акценты не видны", c)
    assert T.contrast(c["card"], c["bg"]) >= 1.03, ("карточка сливается с фоном", c)


# ----------------------------------------------------------------------------- правила ввода и контраст
assert T.norm_hex("#ABC") == "#aabbcc" and T.norm_hex(" #AABBCC ") == "#aabbcc" and T.norm_hex("123456") == "#123456"
for bad in ("", "red", "#12", "#12345g", "rgb(1,2,3)", None):
    try:
        T.norm_hex(bad)
        raise AssertionError("цвет должен отклоняться: " + repr(bad))
    except ValueError:
        pass
assert T.clean_name("  Мой   <b>цвет</b> ") == "Мой bцвет/b" and len(T.clean_name("x" * 50)) == 30
assert T.clean_emoji("🌃") == "🌃" and T.clean_emoji("abc", "🎨") == "🎨" and T.clean_emoji("<b>", "🎨") == "🎨" and T.clean_emoji("1", "🎨") == "🎨"
assert T.clean_emoji("🏎️") == "🏎️" and T.clean_emoji("👨‍💻") == "👨‍💻"  # вариационный селектор и ZWJ-последовательность целы
assert T.clean_decor("✨ 🌸 ✨ x <y>") == ["✨", "🌸"] and T.clean_decor(["🎃"] * 20) == ["🎃"] and len(T.clean_decor([chr(0x1F300 + i) for i in range(30)])) == 8
# мусорные палитры исправляются: светлый текст на светлом фоне, тёмный на тёмном, серый «посередине», одинаковые цвета
for colors in (
    {"bg": "#ffffff", "card": "#ffffff", "ink": "#eeeeee", "accent": "#ffff00", "ok": "#99ff99", "bad": "#ffcccc"},
    {"bg": "#000000", "card": "#000000", "ink": "#111111", "accent": "#050505", "ok": "#0a0a0a", "bad": "#101010"},
    {"bg": "#808080", "card": "#8a8a8a", "ink": "#999999", "accent": "#848484", "ok": "#7a7a7a", "bad": "#888888"},
    {"bg": "#777", "card": "#777", "ink": "#777", "accent": "#777", "ok": "#777", "bad": "#777"},
):
    fixed = T.clean_theme({"name": "x", "colors": colors})
    assert_readable(fixed)
    assert fixed["mode"] in ("dark", "light")
assert T.clean_theme(GOOD)["mode"] == "dark" and T.clean_theme(theme(colors={**GOOD["colors"], "bg": "#f4f5fb", "card": "#ffffff", "ink": "#1b1e2b"}))["mode"] == "light"
good = T.clean_theme(GOOD)
assert good["colors"] == GOOD["colors"], "хорошая палитра не должна меняться"
for broken in (None, [], {"name": "x"}, {"name": "x", "colors": {"bg": "#000"}}, {"name": "", "colors": GOOD["colors"]}, {"name": "x", "colors": {**GOOD["colors"], "ink": "no"}}):
    try:
        T.clean_theme(broken)
        raise AssertionError("тема должна отклоняться: " + repr(broken))
    except ValueError:
        pass
print("OK: проверка ввода и автоисправление контраста")

# ----------------------------------------------------------------------------- CRUD
anon = httpx.Client(base_url=BASE, timeout=30)
assert anon.get("/api/themes").status_code == 401 and anon.post("/api/themes", json=GOOD).status_code == 401
assert anon.post("/api/themes/generate", json={"prompt": "море"}).status_code == 401
a, ua = reg("thema")
b, ub = reg("themb")
r = a.get("/api/themes").json()
assert r["items"] == [] and r["max"] == T.MAX_THEMES and r["gen_left"] == {"hour": T.GEN_PER_HOUR, "day": T.GEN_PER_DAY}, r
assert a.get("/api/me").json()["theme_custom"] is None

r = a.post("/api/themes", json=GOOD)
assert r.status_code == 200, r.text
t1 = r.json()
assert t1["name"] == "Ночной Токио" and t1["mode"] == "dark" and t1["colors"] == GOOD["colors"] and t1["logo"] == "🌃" and t1["decor"] == ["✨", "🌸"], t1
# сервер сам исправляет нечитаемое
r = a.post("/api/themes", json=theme(name="Сливается", colors={"bg": "#ffffff", "card": "#ffffff", "ink": "#eeeeee", "accent": "#ffff00", "ok": "#99ff99", "bad": "#ffcccc"}))
assert r.status_code == 200, r.text
assert_readable(r.json())
t2 = r.json()
# плохой ввод
for body in ({**GOOD, "colors": {**GOOD["colors"], "bg": "red"}}, {**GOOD, "name": "   "}, {**GOOD, "colors": {"bg": "#000000"}},
             {**GOOD, "extra": 1}, {"name": "x"}, {**GOOD, "logo": "x" * 40}):
    assert a.post("/api/themes", json=body).status_code in (400, 422), body
# некорректный эмодзи и декор заменяются безопасными значениями, а не попадают в базу
r = a.post("/api/themes", json=theme(name="Эмодзи", logo="<script>", decor=["<img>", "🌙", "abc", "🌙"])).json()
assert r["logo"] == "🎨" and r["decor"] == ["🌙"], r
a.delete(f"/api/themes/{r['id']}")
assert [x["id"] for x in a.get("/api/themes").json()["items"]] == [t1["id"], t2["id"]]

# правка
r = a.put(f"/api/themes/{t1['id']}", json=theme(name="Токио v2", decor=[]))
assert r.status_code == 200 and r.json()["name"] == "Токио v2" and r.json()["decor"] == [] and r.json()["id"] == t1["id"], r.text
assert a.put("/api/themes/999999", json=GOOD).status_code == 404
assert a.put(f"/api/themes/{t1['id']}", json={**GOOD, "colors": {**GOOD["colors"], "bg": "red"}}).status_code == 400
# чужие темы недоступны
assert b.put(f"/api/themes/{t1['id']}", json=GOOD).status_code == 404 and b.delete(f"/api/themes/{t1['id']}").status_code == 404
assert b.get("/api/themes").json()["items"] == []
assert a.get("/api/themes").json()["items"][0]["name"] == "Токио v2"
print("OK: создание, правка, чужие темы")

# ----------------------------------------------------------------------------- выбор активной темы в профиле
extra = {"preset": "", "bg": "glow", "radius": "normal", "motion": "full"}
assert a.put("/api/me", json={"theme_extra": {**extra, "custom": 999999}}).status_code == 400      # такой темы нет
assert a.put("/api/me", json={"theme_extra": {**extra, "custom": t1["id"]}}).status_code == 200
assert b.put("/api/me", json={"theme_extra": {**extra, "custom": t1["id"]}}).status_code == 400      # чужая тема
m = a.get("/api/me").json()
assert m["theme_extra"]["custom"] == t1["id"] and m["theme_custom"]["id"] == t1["id"] and m["theme_custom"]["name"] == "Токио v2", m
assert b.get("/api/me").json()["theme_custom"] is None
assert a.put("/api/me", json={"theme_extra": {**extra, "custom": 0}}).status_code == 422
# правка активной темы сразу видна в профиле
a.put(f"/api/themes/{t1['id']}", json=theme(name="Токио v3"))
assert a.get("/api/me").json()["theme_custom"]["name"] == "Токио v3"
# выбрать «обычную» тему обратно (custom не передаётся → сбрасывается)
assert a.put("/api/me", json={"theme_extra": {**extra, "preset": "neon"}}).status_code == 200
m = a.get("/api/me").json()
assert m["theme_custom"] is None and m["theme_extra"]["custom"] is None and m["theme_extra"]["preset"] == "neon", m
# удаление активной темы сбрасывает выбор
a.put("/api/me", json={"theme_extra": {**extra, "custom": t2["id"]}})
assert a.get("/api/me").json()["theme_custom"]["id"] == t2["id"]
assert a.delete(f"/api/themes/{t2['id']}").status_code == 200
m = a.get("/api/me").json()
assert m["theme_custom"] is None and m["theme_extra"]["custom"] is None, m
assert a.delete(f"/api/themes/{t2['id']}").status_code == 404
print("OK: активная тема в профиле")

# ----------------------------------------------------------------------------- лимит тем
c, uc = reg("themc")
for i in range(T.MAX_THEMES):
    assert c.post("/api/themes", json=theme(name=f"Тема {i}")).status_code == 200
r = c.post("/api/themes", json=GOOD)
assert r.status_code == 409 and str(T.MAX_THEMES) in r.json()["detail"], r.text
first = c.get("/api/themes").json()["items"][0]["id"]
c.delete(f"/api/themes/{first}")
assert c.post("/api/themes", json=GOOD).status_code == 200
print("OK: лимит тем")

# ----------------------------------------------------------------------------- генерация по описанию
d, ud = reg("themd")
assert d.post("/api/themes/generate", json={"prompt": "  "}).status_code in (400, 422)
assert d.post("/api/themes/generate", json={"prompt": "аб"}).status_code == 400
assert d.post("/api/themes/generate", json={"prompt": "море", "extra": 1}).status_code == 422
seen = set()
for i in range(3):
    r = d.post("/api/themes/generate", json={"prompt": "ночной Токио, неон" if i < 2 else "светлая, как страница старой книги"})
    assert r.status_code == 200, r.text
    t = r.json()["theme"]
    assert_readable(t)
    assert set(t) == {"name", "mode", "colors", "logo", "decor"} and t["name"] and t["mode"] in ("dark", "light"), t
    assert r.json()["gen_left"]["hour"] == T.GEN_PER_HOUR - 1 - i, r.json()
    seen.add(t["colors"]["bg"])
assert t["mode"] == "light", t   # мок: слово «светлая» даёт светлую тему
assert len(seen) >= 2, "разные описания должны давать разные темы"
assert d.get("/api/themes").json()["items"] == [], "генерация ничего не сохраняет"
# черновик сохраняется обычным POST
assert d.post("/api/themes", json=t).status_code == 200
# лимит в час
n_left = T.GEN_PER_HOUR - 3
for _ in range(n_left):
    assert d.post("/api/themes/generate", json={"prompt": "лес и туман"}).status_code == 200
r = d.post("/api/themes/generate", json={"prompt": "лес и туман"})
assert r.status_code == 429 and "в час" in r.json()["detail"], r.text
assert d.get("/api/themes").json()["gen_left"]["hour"] == 0
# другой игрок не затронут; через час лимит восстанавливается
assert a.post("/api/themes/generate", json={"prompt": "море"}).status_code == 200
sql("UPDATE theme_gen SET ts=ts-4000 WHERE user_id=?", (ud["id"],))
assert d.post("/api/themes/generate", json={"prompt": "лес и туман"}).status_code == 200
# суточный лимит
sql("DELETE FROM theme_gen WHERE user_id=?", (ud["id"],))
sql("INSERT INTO theme_gen(user_id, ts) VALUES " + ",".join(f"({ud['id']}, {time.time() - 7200 - i})" for i in range(T.GEN_PER_DAY)))
r = d.post("/api/themes/generate", json={"prompt": "лес и туман"})
assert r.status_code == 429 and "в сутки" in r.json()["detail"], r.text
print("OK: генерация по описанию и лимиты")

# ----------------------------------------------------------------------------- удаление пользователя
adm = httpx.post(f"{BASE}/api/auth/login", json={"username": os.environ.get("ADMIN_USERNAME", "") or "x", "password": "x"}, timeout=30)
sql("UPDATE users SET is_admin=1 WHERE id=?", (ua["id"],))
assert a.delete(f"/api/admin/users/{ub['id']}").status_code in (200, 204), "администратор удаляет пользователя"
sql("INSERT INTO user_themes(user_id, name, data) VALUES (?,?,?)", (ud["id"], "x", "{}"))
assert a.delete(f"/api/admin/users/{ud['id']}").status_code in (200, 204)
assert sql("SELECT COUNT(*) FROM user_themes WHERE user_id=?", (ud["id"],))[0][0] == 0
assert sql("SELECT COUNT(*) FROM theme_gen WHERE user_id=?", (ud["id"],))[0][0] == 0
assert sql("SELECT COUNT(*) FROM user_themes WHERE user_id=?", (ua["id"],))[0][0] >= 1, "темы других не тронуты"
print("OK: удаление пользователя чистит темы")
print("ВСЕ ПРОВЕРКИ ТЕМ ПРОЙДЕНЫ")
