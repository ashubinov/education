"""Telegram-бот: обучение прямо в мессенджере. Long polling (работает с домашнего ПК, без сервера и вебхуков)."""
import asyncio
import html
import json
import re
import traceback

import httpx

from . import config, engine, generator, gamification as gm, service, svgtools
from .db import db, jl

TRANSPORT = None  # httpx-транспорт для тестов
API_BASE = "https://api.telegram.org"
_task: asyncio.Task | None = None
_username = ""
_error = ""
_client: httpx.AsyncClient | None = None
_chat_locks: dict[int, asyncio.Lock] = {}
_pending: set[asyncio.Task] = set()

LETTERS = "ABCDEFGH"
BAR = "▁▂▃▄▅▆▇█"


def token() -> str:
    return (db.get_setting("telegram_token") or config.env("TELEGRAM_BOT_TOKEN")).strip()


def bot_username() -> str:
    return _username


def last_error() -> str:
    return _error


def esc(s) -> str:
    return html.escape(str(s or ""))


# --------------------------------------------------------------------- низкоуровневое API
async def api(method: str, **params):
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(connect=15, read=60, write=30, pool=30), transport=TRANSPORT)
    r = await _client.post(f"{API_BASE}/bot{token()}/{method}", json=params)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data.get('description')}")
    return data["result"]


async def send(chat_id: int, text: str, buttons: list[list[tuple[str, str]]] | None = None, **kw):
    params = {"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        params["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]}
    params.update(kw)
    try:
        return await api("sendMessage", **params)
    except RuntimeError as e:
        if "can't parse" in str(e):
            params.pop("parse_mode", None)
            params["text"] = re.sub(r"<[^>]+>", "", params["text"])
            return await api("sendMessage", **params)
        raise


async def edit(chat_id: int, message_id: int, text: str, buttons=None):
    params = {"chat_id": chat_id, "message_id": message_id, "text": text[:4000], "parse_mode": "HTML"}
    params["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in (buttons or [])]}
    try:
        await api("editMessageText", **params)
    except RuntimeError:
        pass


async def send_photo(chat_id: int, png: bytes, caption: str, buttons=None):
    global _client
    data = {"chat_id": str(chat_id), "caption": caption[:1000], "parse_mode": "HTML"}
    if buttons:
        data["reply_markup"] = json.dumps({"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]})
    r = await _client.post(f"{API_BASE}/bot{token()}/sendPhoto", data=data, files={"photo": ("q.png", png, "image/png")})
    return r.json()


# --------------------------------------------------------------------- вспомогательное
def user_by_chat(chat_id: int) -> dict | None:
    return db.one("SELECT * FROM users WHERE tg_chat_id=?", (chat_id,))


MENU = [[("📚 Мои курсы", "menu:courses")], [("📊 Статистика", "menu:stats"), ("🔥 Серия", "menu:streak")]]


def pbar(pct: int, n: int = 10) -> str:
    f = round(pct / 100 * n)
    return "🟩" * f + "⬜" * (n - f)


def render_course(c: dict, u: dict) -> tuple[str, list]:
    from .main import course_progress  # избегаем циклического импорта на старте
    p = course_progress(c)
    g = gm.goal_info(c, u["id"])
    s = gm.streak_info(u["id"], c["id"])
    lines = [f"{esc(c['icon'])} <b>{esc(c['title'])}</b>", f"{pbar(p['percent'])} {p['percent']}%  ·  уроков {p['lessons_done']}/{p['lessons_total']}",
             f"⭐ {gm.total_xp(u['id'], c['id'])} XP   🔥 серия {s['current']} дн."]
    if g:
        if g["status"] == "done":
            lines.append("🎯 Цель достигнута!")
        else:
            lines.append(f"🎯 До {g['date']}: {g['days_left']} дн. · нужно ≈ {g['per_day_minutes']} мин/день (сегодня {g['spent_today']:g})")
    btns = [[("▶️ Продолжить", f"go:{c['id']}")], [("⬅️ К курсам", "menu:courses")]]
    return "\n".join(lines), btns


def render_step(v: dict) -> tuple[str, list]:
    st, total = v["step"], v["total"]
    head = f"<i>{esc(v['lesson']['title'])}</i>  ·  шаг {st['idx'] + 1}/{total}"
    rid = v["run_id"]
    k = st["kind"]
    if k == "read":
        txt = f"{head}\n\n📖 <b>{esc(st['term'])}</b>\n{esc(st['definition'])}"
        if st.get("example"):
            txt += f"\n\n💡 <i>{esc(st['example'])}</i>"
        return txt, [[("Запомнил ▶️", f"r:{rid}")]]
    if k in ("single", "multi", "image"):
        opts = "\n".join(f"<b>{LETTERS[i]}.</b> {esc(o)}" for i, o in enumerate(st["options"]))
        hint = "\n<i>(можно выбрать несколько, затем «Ответить»)</i>" if k == "multi" else ""
        txt = f"{head}\n\n❓ {esc(st['prompt'])}{hint}\n\n{opts}"
        if k == "multi":
            rows = [[(LETTERS[i], f"t:{rid}:{i}") for i in range(len(st["options"]))], [("✅ Ответить", f"ok:{rid}")]]
        else:
            rows = [[(LETTERS[i], f"a:{rid}:{i}") for i in range(len(st["options"]))]]
        return txt, rows
    if k == "fill":
        return f"{head}\n\n✏️ {esc(st['prompt'])}\n\n<i>Напиши пропущенное сообщением.</i>", []
    if k == "write":
        if st["stage"] == "tail":
            return f"{head}\n\n✍️ <b>{esc(st['term'])}</b> — {esc(st['head'])} …\n\n<i>Допиши остаток определения сообщением.</i>", []
        return f"{head}\n\n✍️ Напиши определение термина <b>{esc(st['term'])}</b> сообщением.", []
    if k == "task":
        txt = f"{head}\n\n🛠 <b>{esc(st['title'])}</b>\n\n{esc(st['statement'])}"
        if st.get("starter"):
            txt += f"\n\n<pre>{esc(st['starter'])}</pre>"
        if st.get("hints"):
            txt += "\n\n💡 Подсказки: " + " / ".join(esc(h) for h in st["hints"][:2])
        return txt + "\n\n<i>Пришли решение сообщением.</i>", [[("Пропустить", f"skip:{rid}")]]
    return head, []


async def send_current(chat_id: int, user: dict, rid: int):
    run, lesson, state = engine.get_run(user["id"], rid)
    v = engine.run_view(run, lesson, state)
    if not v["step"]:
        await finish_run(chat_id, user, rid)
        return
    st = v["step"]
    text, rows = render_step(v)
    needs_text = st["kind"] in ("fill", "write", "task")
    db.x("UPDATE users SET tg_run_id=? WHERE id=?", (rid if needs_text else None, user["id"]))
    state["sel"] = []
    db.x("UPDATE runs SET state=? WHERE id=?", (json.dumps(state, ensure_ascii=False), rid))
    if st["kind"] == "image" and st.get("svg"):
        png = svgtools.to_png(st["svg"])
        if png:
            await send_photo(chat_id, png, text[:900], rows)
            return
        text += "\n\n🖼 (картинка доступна в веб-версии)"
    await send(chat_id, text, rows or None)


def feedback_text(res: dict) -> str:
    k = res["kind"]
    if k == "read":
        return ""
    if res.get("self_check"):
        return f"{res['feedback']}\n\n<b>Эталон:</b>\n{esc(res['correct_answer'])}"
    mark = "✅ <b>Верно!</b>" if res.get("correct") else "🟡 <b>Почти</b>" if res.get("partial") else "❌ <b>Неверно</b>"
    lines = [f"{mark}  +{res['xp']} XP"]
    if k == "task":
        lines.append(esc(res.get("feedback", "")))
        if not res.get("correct"):
            lines.append(f"<b>Эталон:</b>\n{esc(res.get('correct_answer', ''))}")
    elif not res.get("correct"):
        lines.append(f"Правильный ответ: <b>{esc(res.get('correct_answer', ''))}</b>")
    if res.get("explanation"):
        lines.append(f"💬 {esc(res['explanation'])}")
    if res.get("requeued"):
        lines.append("🔁 Вернёмся к этому в конце урока.")
    return "\n".join(lines)


async def after_answer(chat_id: int, user: dict, rid: int, res: dict):
    text = feedback_text(res)
    btns = []
    if res.get("self_check"):
        btns = [[("❌ Не решил", f"s:{rid}:0"), ("🟡 Частично", f"s:{rid}:0.5"), ("✅ Решил", f"s:{rid}:1")]]
        await send(chat_id, text, btns)
        db.x("UPDATE users SET tg_run_id=NULL WHERE id=?", (user["id"],))
        return
    if res.get("can_override"):
        btns.append([("Засчитать мне ✅", f"o:{rid}")])
    btns.append([("Дальше ▶️", f"n:{rid}")])
    db.x("UPDATE users SET tg_run_id=NULL WHERE id=?", (user["id"],))
    await send(chat_id, text or "Дальше", btns)


async def finish_run(chat_id: int, user: dict, rid: int):
    run, lesson, state = engine.get_run(user["id"], rid)
    first = not run["finished_at"]
    s = engine.finish_run(user["id"], rid)
    if first:
        generator.spawn(generator.after_lesson(lesson["id"]))
    db.x("UPDATE users SET tg_run_id=NULL WHERE id=?", (user["id"],))
    li = s["level"]
    lines = [f"🎉 <b>Урок пройден!</b>  {esc(lesson['title'])}",
             f"Результат: <b>{round(s['score'] * 100)}%</b>  ·  +{s['xp_total_gained']} XP",
             f"⭐ Уровень {li['level']}  ({li['into']}/{li['span']} XP)   🔥 Серия: {s['streak']['current']} дн."]
    if s["level_up"]:
        lines.append(f"🚀 <b>Новый уровень — {li['level']}!</b>")
    for a in s["achievements"]:
        lines.append(f"🏅 Достижение: {a['icon']} <b>{esc(a['title'])}</b> — {esc(a['desc'])}")
    msg = None
    mod = db.one("SELECT plan FROM modules WHERE id=?", (lesson["module_id"],))
    if lesson["type"] == "final_test" and mod:
        msg = (jl(mod["plan"], {}) or {}).get("message")
    if msg:
        lines.append(f"💬 {esc(msg)}")
    await send(chat_id, "\n".join(lines), [[("▶️ Следующий урок", f"go:{lesson['course_id']}")], [("📚 Курсы", "menu:courses")]])


async def go_course(chat_id: int, user: dict, cid: int):
    c = db.one("SELECT * FROM courses WHERE id=? AND user_id=?", (cid, user["id"]))
    if not c:
        await send(chat_id, "Курс не найден.")
        return
    if c["status"] == "processing":
        await send(chat_id, f"⏳ Курс ещё собирается: {esc(c['status_text'] or 'подожди немного')}.", [[("Обновить", f"go:{cid}")]])
        return
    if c["status"] == "error":
        await send(chat_id, f"⚠️ Не удалось собрать курс: {esc(c['error'])}\nНажми «Повторить» в веб-версии.")
        return
    notice = None
    for attempt in range(80):  # до ~2 минут ожидания генерации
        c = db.one("SELECT * FROM courses WHERE id=?", (cid,))
        if c["status"] == "completed":
            await send(chat_id, "🏆 Курс пройден полностью! Поздравляю.", [[("📚 Курсы", "menu:courses")]])
            return
        l = generator.next_lesson(cid)
        if not l:
            nm = generator.next_unplanned_module(cid)
            if nm and not generator.lock(f"module:{nm['id']}").locked():
                generator.spawn(generator.prepare_next(cid))
            if not nm and attempt > 2:
                await send(chat_id, "🏆 Курс пройден полностью!", [[("📚 Курсы", "menu:courses")]])
                return
        elif l["status"] == "ready":
            run = engine.start_run(user["id"], l)
            if notice:
                await edit(chat_id, notice["message_id"], f"✅ Урок готов: <b>{esc(l['title'])}</b>")
            await send_current(chat_id, user, run["id"])
            return
        elif l["status"] == "failed":
            await send(chat_id, f"⚠️ {esc(l['error'])}", [[("Повторить", f"go:{cid}")]])
            db.x("UPDATE lessons SET status='pending' WHERE id=?", (l["id"],))
            return
        elif l["status"] == "pending":
            generator.spawn(generator.generate_lesson(l["id"]))
        if not notice:
            notice = await send(chat_id, "⏳ Готовлю для тебя урок…")
        try:
            await api("sendChatAction", chat_id=chat_id, action="typing")
        except Exception:
            pass
        await asyncio.sleep(1.5)
    await send(chat_id, "Урок ещё готовится, нажми «Продолжить» через минуту.", [[("▶️ Продолжить", f"go:{cid}")]])


async def show_courses(chat_id: int, user: dict):
    from .main import course_progress
    rows = db.q("SELECT * FROM courses WHERE user_id=? ORDER BY COALESCE(last_opened, created_at) DESC", (user["id"],))
    if not rows:
        await send(chat_id, "У тебя пока нет курсов. Пришли мне файл (PDF, DOCX, TXT, MD) — я соберу по нему курс. "
                            "Можно также добавить курс в веб-версии.")
        return
    btns = [[(f"{c['icon']} {c['title'][:34]} — {course_progress(c)['percent']}%", f"c:{c['id']}")] for c in rows[:12]]
    await send(chat_id, "📚 <b>Твои курсы</b>\nВыбери курс (или пришли файл, чтобы создать новый):", btns)


async def show_stats(chat_id: int, user: dict):
    xp = gm.total_xp(user["id"])
    li = gm.level_info(xp)
    st = gm.streak_info(user["id"])
    wk = gm.week(user["id"])
    mx = max([d["xp"] for d in wk] + [1])
    lines = [f"⭐ <b>Уровень {li['level']}</b>  ·  {xp} XP  ({li['into']}/{li['span']} до следующего)",
             f"🔥 Серия: <b>{st['current']}</b> дн.  (рекорд {st['best']})", "", "<b>Неделя</b> (XP / уроки):"]
    for d in wk:
        bar = BAR[min(7, round(d["xp"] / mx * 7))] * 3 if d["xp"] else "···"
        lines.append(f"{d['label']} {bar} {d['xp']} · {d['lessons']}{' ◀ сегодня' if d['today'] else ''}")
    ach = [a for a in gm.achievements_list(user["id"]) if a["unlocked"]]
    if ach:
        lines.append("\n🏅 " + " ".join(a["icon"] for a in ach))
    await send(chat_id, "\n".join(lines), [[("📚 Курсы", "menu:courses")]])


# --------------------------------------------------------------------- обработчики
async def on_message(msg: dict):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    user = user_by_chat(chat_id)

    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        code = parts[1].strip().upper() if len(parts) > 1 else ""
        if code:
            u = db.one("SELECT * FROM users WHERE tg_link_code=?", (code,))
            if not u:
                await send(chat_id, "Код не найден или устарел. Сгенерируй новый в веб-версии: Настройки → Telegram.")
                return
            db.x("UPDATE users SET tg_chat_id=NULL WHERE tg_chat_id=?", (chat_id,))
            db.x("UPDATE users SET tg_chat_id=?, tg_link_code=NULL WHERE id=?", (chat_id, u["id"]))
            new = gm.check_achievements(u["id"])
            await send(chat_id, f"✅ Аккаунт <b>{esc(u['display_name'] or u['username'])}</b> подключён! Теперь можно учиться прямо здесь.\n"
                                + "".join(f"\n🏅 {a['icon']} {esc(a['title'])}" for a in new), MENU)
            return
        if user:
            await send(chat_id, f"👋 С возвращением, {esc(user['display_name'])}!", MENU)
        else:
            await send(chat_id, "👋 Это бот приложения LearnQuest.\nЧтобы подключить аккаунт: открой веб-версию → Настройки → Telegram → "
                                "«Подключить» и пришли мне полученный код: <code>/start КОД</code>.")
        return

    if not user:
        await send(chat_id, "Сначала подключи аккаунт: веб-версия → Настройки → Telegram. Затем <code>/start КОД</code>.")
        return

    if text in ("/menu", "/help"):
        await send(chat_id, "Команды:\n/courses — курсы\n/stats — статистика\n/streak — серия\n/remind 19:30 — время напоминания (или /remind off)\n/unlink — отключить аккаунт\n\n"
                            "Чтобы создать курс — пришли файл (PDF, DOCX, TXT, MD).", MENU)
    elif text == "/courses":
        await show_courses(chat_id, user)
    elif text in ("/stats", "/streak"):
        await show_stats(chat_id, user)
    elif text.startswith("/remind"):
        arg = text[7:].strip().lower()
        if arg in ("off", "выкл"):
            db.x("UPDATE users SET reminders_on=0 WHERE id=?", (user["id"],))
            await send(chat_id, "🔕 Напоминания выключены.")
        elif re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", arg):
            hh, mm = arg.split(":")
            db.x("UPDATE users SET reminders_on=1, reminder_time=? WHERE id=?", (f"{int(hh):02d}:{mm}", user["id"]))
            await send(chat_id, f"🔔 Буду напоминать каждый день в {int(hh):02d}:{mm}.")
        else:
            await send(chat_id, f"Сейчас: {'вкл' if user['reminders_on'] else 'выкл'}, {user['reminder_time']}. Пример: /remind 19:30 или /remind off")
    elif text == "/unlink":
        db.x("UPDATE users SET tg_chat_id=NULL, tg_run_id=NULL WHERE id=?", (user["id"],))
        await send(chat_id, "Аккаунт отключён. Вернуться можно через код из веб-версии.")
    elif msg.get("document"):
        await on_document(chat_id, user, msg)
    elif text and not text.startswith("/") and user.get("tg_run_id"):
        await on_text_answer(chat_id, user, text)
    elif text and not text.startswith("/"):
        await send(chat_id, "Выбери действие:", MENU)


async def on_document(chat_id: int, user: dict, msg: dict):
    doc = msg["document"]
    if doc.get("file_size", 0) > 20 * 1024 * 1024:
        await send(chat_id, "Файл слишком большой для бота (лимит Telegram — 20 МБ). Загрузи его в веб-версии.")
        return
    await send(chat_id, f"📥 Получил «{esc(doc.get('file_name', 'файл'))}», читаю…")
    try:
        info = await api("getFile", file_id=doc["file_id"])
        r = await _client.get(f"{API_BASE}/file/bot{token()}/{info['file_path']}")
        r.raise_for_status()
        cid, warns = await asyncio.to_thread(service.create_course, user["id"], [(doc.get("file_name") or "file.txt", r.content)], msg.get("caption", ""))
    except ValueError as e:
        await send(chat_id, f"⚠️ {esc(e)}")
        return
    await send(chat_id, "🧠 Составляю курс — это займёт около минуты. Напишу, когда будет готово.")
    for _ in range(120):
        await asyncio.sleep(3)
        c = db.one("SELECT * FROM courses WHERE id=?", (cid,))
        if not c or c["status"] == "processing":
            continue
        if c["status"] == "error":
            await send(chat_id, f"⚠️ Не получилось собрать курс: {esc(c['error'])}")
        else:
            n = db.val("SELECT COUNT(*) FROM modules WHERE course_id=?", (cid,), 0)
            await send(chat_id, f"✅ Курс готов: {esc(c['icon'])} <b>{esc(c['title'])}</b> — модулей: {n}", [[("▶️ Начать", f"go:{cid}")]])
        return


async def on_text_answer(chat_id: int, user: dict, text: str):
    rid = user["tg_run_id"]
    try:
        run, lesson, state = engine.get_run(user["id"], rid)
        if state.get("task_wait"):
            await send(chat_id, "Оцени себя кнопками выше 👆")
            return
        res = await engine.submit(user["id"], rid, {"text": text})
    except (ValueError, KeyError) as e:
        await send(chat_id, f"⚠️ {esc(e)}")
        return
    await after_answer(chat_id, user, rid, res)


async def on_callback(cb: dict):
    chat_id = cb["message"]["chat"]["id"]
    mid = cb["message"]["message_id"]
    data = cb.get("data", "")
    user = user_by_chat(chat_id)
    try:
        await api("answerCallbackQuery", callback_query_id=cb["id"])
    except Exception:
        pass
    if not user:
        return
    kind, *rest = data.split(":")
    try:
        if kind == "menu":
            if rest[0] == "courses":
                await show_courses(chat_id, user)
            else:
                await show_stats(chat_id, user)
        elif kind == "c":
            c = db.one("SELECT * FROM courses WHERE id=? AND user_id=?", (int(rest[0]), user["id"]))
            if c:
                text, btns = render_course(c, user)
                await send(chat_id, text, btns)
        elif kind == "go":
            await go_course(chat_id, user, int(rest[0]))
        elif kind == "n":
            await mark_answered(chat_id, mid, cb, {})
            await send_current(chat_id, user, int(rest[0]))
        elif kind == "r":  # прочитан шаг-определение: засчитываем и идём дальше
            rid = int(rest[0])
            await mark_answered(chat_id, mid, cb, {})
            run, lesson, state = engine.get_run(user["id"], rid)
            if not run["finished_at"] and run["step_idx"] < len(engine.all_steps(lesson, state)):
                await engine.submit(user["id"], rid, {})
            await send_current(chat_id, user, rid)
        elif kind == "a":
            rid, idx = int(rest[0]), int(rest[1])
            res = await engine.submit(user["id"], rid, {"choice": idx})
            await mark_answered(chat_id, mid, cb, res)
            await after_answer(chat_id, user, rid, res)
        elif kind == "t":
            rid, idx = int(rest[0]), int(rest[1])
            run, lesson, state = engine.get_run(user["id"], rid)
            sel = set(state.get("sel", []))
            sel ^= {idx}
            state["sel"] = sorted(sel)
            db.x("UPDATE runs SET state=? WHERE id=?", (json.dumps(state, ensure_ascii=False), rid))
            n = len(engine.all_steps(lesson, state)[run["step_idx"]]["options"])
            rows = [[((("✅" if i in sel else LETTERS[i])), f"t:{rid}:{i}") for i in range(n)], [("✅ Ответить", f"ok:{rid}")]]
            await api("editMessageReplyMarkup", chat_id=chat_id, message_id=mid,
                      reply_markup={"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]})
        elif kind == "ok":
            rid = int(rest[0])
            run, lesson, state = engine.get_run(user["id"], rid)
            res = await engine.submit(user["id"], rid, {"choice": state.get("sel", [])})
            await mark_answered(chat_id, mid, cb, res)
            await after_answer(chat_id, user, rid, res)
        elif kind == "o":
            r = engine.override(user["id"], int(rest[0]))
            await send(chat_id, f"👌 Засчитано (+{r['xp']} XP).")
        elif kind == "s":
            rid = int(rest[0])
            res = await engine.submit(user["id"], rid, {"self": float(rest[1])})
            await after_answer(chat_id, user, rid, res)
        elif kind == "skip":
            rid = int(rest[0])
            res = await engine.submit(user["id"], rid, {"skip": True})
            await after_answer(chat_id, user, rid, res)
    except (ValueError, KeyError) as e:
        await send(chat_id, f"⚠️ {esc(e)}")


async def mark_answered(chat_id: int, mid: int, cb: dict, res: dict):
    """Убираем кнопки у отвеченного вопроса, чтобы не нажимали дважды."""
    try:
        await api("editMessageReplyMarkup", chat_id=chat_id, message_id=mid, reply_markup={"inline_keyboard": []})
    except Exception:
        pass


# --------------------------------------------------------------------- цикл
async def _handle(update: dict):
    chat = None
    try:
        if "message" in update:
            chat = update["message"]["chat"]["id"]
        elif "callback_query" in update:
            chat = update["callback_query"]["message"]["chat"]["id"]
        lk = _chat_locks.setdefault(chat, asyncio.Lock())
        async with lk:
            if "message" in update:
                await on_message(update["message"])
            elif "callback_query" in update:
                await on_callback(update["callback_query"])
    except Exception:
        traceback.print_exc()
        if chat:
            try:
                await send(chat, "Что-то пошло не так. Попробуй ещё раз или открой /menu.")
            except Exception:
                pass


async def _run():
    global _username, _error
    offset = 0
    while True:
        tk = token()
        if not tk:
            _error = ""
            await asyncio.sleep(5)
            continue
        try:
            me = await api("getMe")
            _username = me.get("username", "")
            _error = ""
            await api("deleteWebhook", drop_pending_updates=False)
            await api("setMyCommands", commands=[
                {"command": "courses", "description": "Мои курсы"}, {"command": "stats", "description": "Статистика"},
                {"command": "streak", "description": "Серия дней"}, {"command": "remind", "description": "Время напоминания"},
                {"command": "help", "description": "Помощь"}])
            current = tk
            while token() == current:
                ups = await api("getUpdates", offset=offset, timeout=25, allowed_updates=["message", "callback_query"])
                for up in ups:
                    offset = up["update_id"] + 1
                    t = asyncio.create_task(_handle(up))
                    _pending.add(t)
                    t.add_done_callback(_pending.discard)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _error = str(e)[:200]
            await asyncio.sleep(8)


def start():
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_run())


async def stop():
    global _task, _client
    if _task:
        _task.cancel()
        try:
            await _task
        except BaseException:
            pass
        _task = None
    if _client:
        await _client.aclose()
        _client = None


async def restart():
    global _username
    _username = ""
    await stop()
    start()
    for _ in range(20):  # даём боту секунду-две, чтобы узнать username
        await asyncio.sleep(0.3)
        if _username or _error:
            break
