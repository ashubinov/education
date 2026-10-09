"""Напоминания: в веб-интерфейсе (опрос с клиента) и в Telegram (серверный планировщик)."""
import asyncio
import traceback
from datetime import datetime

from . import gamification as gm
from . import tgbot
from .db import db

_task: asyncio.Task | None = None


def _due_now(u: dict, at: str | None = None) -> bool:
    now = datetime.now().strftime("%H:%M")
    return now >= (at or u["reminder_time"] or "19:00")


def _message(u: dict) -> tuple[str, str]:
    st = gm.streak_info(u["id"])
    goal = None
    for c in db.q("SELECT * FROM courses WHERE user_id=? AND goal_date IS NOT NULL AND status IN ('ready')", (u["id"],)):
        g = gm.goal_info(c, u["id"])
        if g and g["status"] == "ok":
            goal = (c, g)
            break
    if st["at_risk"]:
        title = f"🔥 Серия {st['current']} дн. под угрозой!"
        body = "Один короткий урок — и огонь не погаснет."
    else:
        title = "📚 Пора учиться!"
        body = "Пара минут сегодня — и ты ближе к цели."
    if goal:
        c, g = goal
        body += f" «{c['title']}»: сегодня ≈ {g['per_day_minutes']} мин."
    return title, body


def web_reminder(u: dict) -> dict:
    st = gm.streak_info(u["id"])
    due = bool(u["reminders_on"]) and not st["done_today"] and _due_now(u)
    title, body = _message(u)
    return {"due": due, "title": title, "body": body, "streak": st["current"], "time": u["reminder_time"], "done_today": st["done_today"]}


async def _tick():
    today = gm.today()
    for u in db.q("SELECT * FROM users WHERE tg_chat_id IS NOT NULL AND reminders_on=1 AND COALESCE(banned,0)=0"):
        try:
            st = gm.streak_info(u["id"])
            if st["done_today"]:
                continue
            if u["last_reminder_day"] != today and _due_now(u):
                title, body = _message(u)
                await tgbot.send(u["tg_chat_id"], f"<b>{title}</b>\n{body}", [[("📚 Открыть курсы", "menu:courses")]])
                db.x("UPDATE users SET last_reminder_day=? WHERE id=?", (today, u["id"]))
            elif st["at_risk"] and u["last_reminder2_day"] != today and datetime.now().strftime("%H:%M") >= "21:00":
                await tgbot.send(u["tg_chat_id"], f"⏰ <b>Сегодня ещё не занимался</b> — серия {st['current']} дн. сгорит в полночь! Хватит одного урока.",
                                 [[("▶️ Учиться", "menu:courses")]])
                db.x("UPDATE users SET last_reminder2_day=? WHERE id=?", (today, u["id"]))
        except Exception:
            traceback.print_exc()


async def _loop():
    while True:
        try:
            if tgbot.token():
                await _tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(60)


def start():
    global _task
    _task = asyncio.create_task(_loop())


async def stop():
    if _task:
        _task.cancel()
        try:
            await _task
        except BaseException:
            pass
