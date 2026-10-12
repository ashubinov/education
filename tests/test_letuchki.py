"""Летучки (API, сквозной; запускается через tests/run_ci.py на одноразовой базе).

Проверяет: разбор файла лектора (темы, варианты, вопросы, значок ✏️), загрузку и дополнение админом (повтор обновляет, не дублирует),
запрет загрузки для обычного пользователя, список предметов/тем/вариантов без закрытых, проверку ответов (верно / частично / неверно),
итог для анимации (ok / partial / fail), опыт только за первое прорешивание и «следующий» вариант.
"""
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
TAG = str(time.time_ns() % 10**11)

MD = """# Летучки по ОС

Вступление, которое не должно попасть в вопросы.

## Летучка 1. Определение и функции ОС

### Вариант 1

**Вопрос 1 (часть 1). Определение ОС и основные функции ОС.**

**ОС** — программный комплекс, который осуществляет управление функционированием компьютера и обеспечивает взаимодействие устройств, входящих в его состав, с пользователями и прикладными программами.

1. Управление ресурсами компьютера.
2. Распределение ресурсов между устройствами, программами и пользователями.

**Вопрос 2 (часть 2). ✏️ Нарисуйте схему «Простая очередь» и опишите порядок работы.**

Схема: два прямоугольника и три стрелки. Слева очередь задач, справа процессор. Новая задача встаёт в очередь, первая задача выбирается из очереди на процессор после освобождения процессора.

&#91;embedded content: Простая очередь\\]

### Вариант 2

**Вопрос 1 (часть 3). Ядро.**

Ядро — главный управляющий модуль: разделение процессорного времени, управление памятью, оборудованием, файловой системой, правами пользователей.

**Вопрос 2 (часть 4). Многопроцессорная обработка.**

Мультипроцессорная обработка — способ организации вычислительного процесса в системах с несколькими процессорами, где процессы выполняются одновременно, а не попеременно.

## Летучка 2. Процессы и потоки

### Вариант 1

**Вопрос 1 (часть 1). Процесс.**

Процесс — совокупность одного или нескольких одновременно выполняемых потоков; процесс завершён, когда завершены все его потоки.

**Вопрос 2 (часть 2). Поток.**

Поток — фрагмент процесса, выполняющийся одновременно с другими; при создании процесса автоматически создаётся один поток.
"""

SECOND = """## Летучка 3. Новая лекция

### Вариант 1

**Вопрос 1 (часть 1). Состояния потока.**

Три состояния потока: выполнение, ожидание и готовность.

**Вопрос 2 (часть 2). Мультипрограммирование.**

Мультипрограммирование — способ, при котором на одном процессоре попеременно выполняются несколько задач.
"""


def reg(suffix):
    res = httpx.post(BASE + "/api/auth/register", json={"username": f"lt{suffix}{TAG}", "password": "secret1"}, timeout=30)
    assert res.status_code == 200, res.text
    data = res.json()
    return httpx.Client(base_url=BASE, headers={"Authorization": "Bearer " + data["token"]}, timeout=30), data["user"]


def upload(client, text, subject="Тестовый предмет"):
    return client.post("/api/letuchki/admin/upload", files={"file": ("l.md", text.encode("utf-8"), "text/markdown")}, data={"subject": subject})


admin, au = reg("a")
assert au["is_admin"]
user, uu = reg("u")
assert not uu["is_admin"]

# 1) загрузка только для админа, мусор отклоняется
assert upload(user, MD).status_code == 403
assert upload(admin, "просто текст без летучек").status_code == 400
r = upload(admin, MD)
assert r.status_code == 200, r.text
res = r.json()
assert res["added"] == 3 and res["updated"] == 0 and res["topics"] == 2, res
sid = res["subject_id"]
print("OK: файл разобран:", res)

# 2) повторная загрузка обновляет, а дополнение добавляет новую летучку; ничего не дублируется
res = upload(admin, MD).json()
assert res["added"] == 0 and res["updated"] == 3, res
res = upload(admin, SECOND).json()
assert res["added"] == 1 and res["subject_id"] == sid, res
print("OK: повторная загрузка обновляет, дополнение добавляет летучку")

# 3) предметы, темы, варианты — ничего не закрыто
subs = user.get("/api/letuchki/subjects").json()
mine = [s for s in subs if s["id"] == sid][0]
assert mine["topics"] == 3 and mine["sets"] == 4 and mine["done"] == 0, mine
t = user.get(f"/api/letuchki/subjects/{sid}").json()
assert [x["no"] for x in t["topics"]] == [1, 2, 3]
assert [len(x["variants"]) for x in t["topics"]] == [2, 1, 1]
assert t["topics"][0]["title"] == "Определение и функции ОС"
set1 = t["topics"][0]["variants"][0]["set_id"]
s = user.get(f"/api/letuchki/sets/{set1}").json()
assert [q["draw"] for q in s["questions"]] == [False, True], s
assert "embedded" not in str(s) and "Вступление" not in str(s)
assert "answer" not in str(s) and "reference" not in str(s), "эталон не должен уходить до ответа"
assert s["next"]["variant"] == 2 and s["attempts"] == 0
print("OK: предметы/темы/варианты открыты, эталон скрыт до проверки")

# 4) проверка: первое прорешивание — частично верно (один вопрос верный, второй мимо): опыт только за него
good1 = "ОС — программный комплекс, который управляет компьютером и обеспечивает взаимодействие устройств с пользователями и программами. Функции: управление ресурсами компьютера, распределение ресурсов между устройствами, программами и пользователями."
bad2 = "не знаю, что тут рисовать"
r = user.post(f"/api/letuchki/sets/{set1}/check", json={"answers": [good1, bad2]}).json()
assert [i["verdict"] for i in r["items"]] == ["correct", "wrong"], r
assert r["overall"] == "partial" and r["first"] and r["xp"] == 15, r
assert "Простая очередь" not in r["items"][1]["reference"] and "очередь" in r["items"][1]["reference"]
xp0 = user.get("/api/me").json()["level"]["xp"]
assert xp0 == 15, xp0
print("OK: первое прорешивание частично верно → итог partial, опыт 15")

# 5) повторные решения опыта не дают, «лучший» результат растёт, итог ok
good2 = "Схема: два прямоугольника и три стрелки. Слева очередь задач, справа процессор. Новая задача встаёт в очередь, первая задача выбирается из очереди на процессор, когда процессор освободится."
r = user.post(f"/api/letuchki/sets/{set1}/check", json={"answers": [good1, good2]}).json()
assert r["overall"] == "ok" and not r["first"] and r["xp"] == 0 and r["attempts"] == 2, r
assert user.get("/api/me").json()["level"]["xp"] == 15
s = user.get(f"/api/letuchki/sets/{set1}").json()
assert s["best"] == "ok" and s["attempts"] == 2 and s["xp"] == 15, s
print("OK: перерешивание опыт не даёт, лучший итог сохраняется")

# 6) полностью неверно → fail; пустые/неполные ответы
set2 = t["topics"][0]["variants"][1]["set_id"]
r = user.post(f"/api/letuchki/sets/{set2}/check", json={"answers": ["Круговорот — схема разделения времени с квантами и очередью", "яблоки груши бананы и прочие фрукты"]}).json()
assert r["overall"] == "fail" and r["first"] and r["xp"] == 0, r
assert user.post(f"/api/letuchki/sets/{set2}/check", json={"answers": ["только один"]}).status_code == 400
assert user.post("/api/letuchki/sets/999999/check", json={"answers": ["a", "b"]}).status_code == 404
assert httpx.post(BASE + f"/api/letuchki/sets/{set2}/check", json={"answers": ["a", "b"]}).status_code == 401
done = [s for s in user.get("/api/letuchki/subjects").json() if s["id"] == sid][0]["done"]
assert done == 2, done
print("OK: неверные ответы → fail, ошибки ввода и доступ")

# 7) обновление эталона не теряет результаты учеников
upload(admin, MD)
assert user.get(f"/api/letuchki/sets/{set1}").json()["attempts"] == 2
print("ALL OK")
