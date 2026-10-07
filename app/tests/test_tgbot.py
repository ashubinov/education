"""Проверка Telegram-бота на фейковом Telegram API (без сети и без настоящего токена).
Сценарий: привязка по коду -> список курсов -> урок за уроком (кнопки и текстовые ответы) -> итоги -> статистика.
Запуск: LQ_DB=<tmp.db> LLM_MOCK=1 python tests/test_tgbot.py"""
import asyncio
import glob
import json
import os
import sys

import httpx

os.environ["TELEGRAM_BOT_TOKEN"] = "123456:FAKE-TOKEN-FOR-TESTS-xxxxxxxxxxxxxxxxxxxx"
os.environ["LLM_MOCK"] = "1"
sys.path.insert(0, ".")
from server import auth, engine, generator, service, tgbot  # noqa: E402
from server.db import db, jl  # noqa: E402

CHAT = 777
sent: list[dict] = []          # все исходящие сообщения бота
queue: list[dict] = []         # входящие обновления
uid_counter = [0]


def upd(**kw):
    uid_counter[0] += 1
    return {"update_id": uid_counter[0], **kw}


def say(text):
    queue.append(upd(message={"message_id": uid_counter[0] + 1000, "chat": {"id": CHAT}, "text": text, "from": {"id": CHAT}}))


def press(data, mid=1):
    queue.append(upd(callback_query={"id": f"cb{uid_counter[0]}", "data": data, "message": {"message_id": mid, "chat": {"id": CHAT}, "text": "x"}}))


async def handler(request: httpx.Request):
    method = request.url.path.rsplit("/", 1)[-1]
    body = {}
    if request.headers.get("content-type", "").startswith("application/json"):
        body = json.loads(request.content)
    if method == "getMe":
        return httpx.Response(200, json={"ok": True, "result": {"username": "fake_bot"}})
    if method == "getUpdates":
        if queue:
            ups = list(queue)
            queue.clear()
            return httpx.Response(200, json={"ok": True, "result": ups})
        await asyncio.sleep(0.15)
        return httpx.Response(200, json={"ok": True, "result": []})
    if method == "sendPhoto":  # multipart: достаём текстовые поля
        raw = request.content.decode("utf-8", "ignore")
        import re as _re
        for name in ("caption", "reply_markup"):
            m = _re.search(r'name="%s"\r\n\r\n(.*?)\r\n--' % name, raw, flags=_re.S)
            if m:
                body[name] = json.loads(m.group(1)) if name == "reply_markup" else m.group(1)
    if method in ("sendMessage", "sendPhoto"):
        sent.append({"method": method, **body})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent) + 100}})
    return httpx.Response(200, json={"ok": True, "result": True})


def buttons(msg):
    rows = (msg.get("reply_markup") or {}).get("inline_keyboard") or []
    return [b for row in rows for b in row]


async def settle(n_before, timeout=6.0):
    """Ждём, пока бот ответит и затихнет."""
    t = 0.0
    last = n_before
    quiet = 0.0
    while t < timeout:
        await asyncio.sleep(0.1)
        t += 0.1
        if len(sent) != last:
            last, quiet = len(sent), 0.0
        else:
            quiet += 0.1
        if len(sent) > n_before and quiet >= 0.4 and not queue:
            return
    return


def current_step(user_id, rid):
    run = db.one("select * from runs where id=?", (rid,))
    lesson = db.one("select * from lessons where id=?", (run["lesson_id"],))
    state = json.loads(run["state"])
    steps = engine.all_steps(lesson, state)
    return run, (steps[run["step_idx"]] if run["step_idx"] < len(steps) else None)


async def main():
    generator.set_loop(asyncio.get_running_loop())
    tgbot.TRANSPORT = httpx.MockTransport(handler)
    uid = auth.register("tgtester", "secret1", "ТГ")
    db.x("UPDATE users SET tg_link_code='ABCD1234' WHERE id=?", (uid,))
    text = open(glob.glob("../test_files/*.pdf")[0], "rb").read()
    cid, _ = service.create_course(uid, [("lec.pdf", text)], "Тест TG")
    for _ in range(100):
        await asyncio.sleep(0.2)
        if db.one("select status from courses where id=?", (cid,))["status"] == "ready":
            break
    tgbot.start()
    await asyncio.sleep(0.5)
    assert tgbot.bot_username() == "fake_bot", tgbot.bot_username()

    # 1) привязка
    say("/start")
    await settle(0)
    assert "Это бот приложения" in sent[-1]["text"], sent[-1]["text"]
    n = len(sent)
    say("/start ABCD1234")
    await settle(n)
    assert "подключён" in sent[-1]["text"], sent[-1]["text"]
    assert db.one("select tg_chat_id from users where id=?", (uid,))["tg_chat_id"] == CHAT

    # 2) курсы
    n = len(sent)
    say("/courses")
    await settle(n)
    btn = buttons(sent[-1])[0]
    assert btn["callback_data"] == f"c:{cid}", btn
    n = len(sent)
    press(btn["callback_data"])
    await settle(n)
    assert "Продолжить" in json.dumps(sent[-1], ensure_ascii=False)

    # 3) проходим уроки
    n = len(sent)
    press(f"go:{cid}")
    lessons_done = 0
    for _ in range(400):
        await settle(n, 8)
        n = len(sent)
        msg = sent[-1]
        bs = buttons(msg)
        data = [b["callback_data"] for b in bs]
        txt = msg["text"] if "text" in msg else msg.get("caption", "")
        print("  bot>", txt.replace("\n", " | ")[:110], "|", [d[:14] for d in data][:6], flush=True)
        if "Урок пройден" in txt:
            lessons_done += 1
            print("   ", txt.replace("\n", " | ")[:150])
            if lessons_done >= 4:
                break
            press(f"go:{cid}")
            continue
        if any(d[:2] in ("n:", "r:") for d in data):
            press(next(d for d in data if d[:2] in ("n:", "r:")))
        elif any(d.startswith("a:") for d in data):
            rid = int(data[0].split(":")[1])
            run, st = current_step(uid, rid)
            press(f"a:{rid}:{st['answer']}")
        elif any(d.startswith("t:") for d in data):
            rid = int(data[0].split(":")[1])
            run, st = current_step(uid, rid)
            for i in st["answer"]:
                press(f"t:{rid}:{i}")
                await asyncio.sleep(0.3)
            press(f"ok:{rid}")
        elif any(d.startswith("go:") for d in data):
            press(next(d for d in data if d.startswith("go:")))
        elif any(d.startswith("s:") for d in data):
            press(next(d for d in data if d.startswith("s:") and d.endswith(":1")))
        else:
            u = db.one("select * from users where id=?", (uid,))
            if u["tg_run_id"]:
                run, st = current_step(uid, u["tg_run_id"])
                k = st["step"]
                if k == "fill":
                    say(st["accept"][0])
                elif k == "write":
                    say(st["reference"])
                elif k == "task":
                    say("Подробное решение: связываю понятия и привожу пример.")
                else:
                    raise SystemExit(f"неожиданный шаг {k}")
            else:
                raise SystemExit("бот ждёт, но ответ не распознан: " + json.dumps(msg, ensure_ascii=False)[:300])
    assert lessons_done >= 4, lessons_done

    # 4) статистика и напоминания
    n = len(sent)
    say("/stats")
    await settle(n)
    assert "Уровень" in sent[-1]["text"] and "Серия" in sent[-1]["text"], sent[-1]["text"]
    print(sent[-1]["text"][:400])
    n = len(sent)
    say("/remind 08:15")
    await settle(n)
    assert db.one("select reminder_time from users where id=?", (uid,))["reminder_time"] == "08:15"
    await tgbot.stop()
    print("OK: tgbot (сообщений бота:", len(sent), ")")


asyncio.run(main())
