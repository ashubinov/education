"""FastAPI-приложение LearnQuest."""
import asyncio
import re
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import auth, config, engine, generator, notifier, service, tgbot
from . import gamification as gm
from .db import db, jd, jl
from .llm import LLMError, llm


@asynccontextmanager
async def lifespan(app: FastAPI):
    generator.set_loop(asyncio.get_running_loop())
    # восстановление после перезапуска
    db.x("UPDATE lessons SET status='pending' WHERE status='generating'")
    for c in db.q("SELECT id FROM courses WHERE status='processing'"):
        generator.spawn(generator.build_course(c["id"]))
    tgbot.start()
    notifier.start()
    yield
    await tgbot.stop()
    await notifier.stop()


app = FastAPI(title="LearnQuest", lifespan=lifespan)


@app.middleware("http")
async def no_cache(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/static") or request.url.path == "/":
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.exception_handler(ValueError)
async def value_error(_, exc: ValueError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(KeyError)
async def key_error(_, exc: KeyError):
    return JSONResponse({"detail": "Не найдено"}, status_code=404)


def me(request: Request) -> dict:
    return auth.require_user(request)


def own_course(user: dict, course_id: int) -> dict:
    c = db.one("SELECT * FROM courses WHERE id=? AND user_id=?", (course_id, user["id"]))
    if not c:
        raise HTTPException(404, "Курс не найден")
    return c


# =============================================================== профиль
def user_public(u: dict) -> dict:
    xp = gm.total_xp(u["id"])
    return {
        "id": u["id"], "username": u["username"], "display_name": u["display_name"] or u["username"], "avatar": u["avatar"],
        "theme_color": u["theme_color"], "theme_mode": u["theme_mode"], "sound": bool(u["sound"]),
        "reminders_on": bool(u["reminders_on"]), "reminder_time": u["reminder_time"], "is_admin": bool(u["is_admin"]),
        "telegram": bool(u["tg_chat_id"]), "level": gm.level_info(xp), "streak": gm.streak_info(u["id"]),
    }


def _set_cookie(resp: Response, token: str):
    resp.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_TTL, httponly=True, samesite="lax", path="/")


@app.post("/api/auth/register")
async def register(response: Response, body: dict = Body(...)):
    uid = await run_in_threadpool(auth.register, body.get("username"), body.get("password"), body.get("display_name", ""))
    _set_cookie(response, auth.new_session(uid))
    return user_public(db.one("SELECT * FROM users WHERE id=?", (uid,)))


@app.post("/api/auth/login")
async def login(request: Request, response: Response, body: dict = Body(...)):
    ip = request.client.host if request.client else ""
    uid = await run_in_threadpool(auth.login, body.get("username"), body.get("password"), ip)
    _set_cookie(response, auth.new_session(uid))
    return user_public(db.one("SELECT * FROM users WHERE id=?", (uid,)))


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    t = request.cookies.get(auth.COOKIE)
    if t:
        auth.drop_session(t)
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
async def get_me(request: Request):
    u = auth.user_from_request(request)
    if not u:
        raise HTTPException(401, "Нужно войти")
    return user_public(u)


@app.put("/api/me")
async def update_me(request: Request, body: dict = Body(...)):
    u = me(request)
    fields = {}
    if "display_name" in body:
        fields["display_name"] = str(body["display_name"]).strip()[:40] or u["username"]
    if "avatar" in body:
        fields["avatar"] = str(body["avatar"])[:8]
    if "theme_color" in body:
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(body["theme_color"])):
            raise HTTPException(400, "Цвет в формате #rrggbb")
        fields["theme_color"] = body["theme_color"]
    if "theme_mode" in body:
        if body["theme_mode"] not in ("dark", "light", "auto"):
            raise HTTPException(400, "Режим: dark/light/auto")
        fields["theme_mode"] = body["theme_mode"]
    if "sound" in body:
        fields["sound"] = int(bool(body["sound"]))
    if "reminders_on" in body:
        fields["reminders_on"] = int(bool(body["reminders_on"]))
    if "reminder_time" in body:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(body["reminder_time"])):
            raise HTTPException(400, "Время в формате ЧЧ:ММ")
        fields["reminder_time"] = body["reminder_time"]
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        db.x(f"UPDATE users SET {sets} WHERE id=?", (*fields.values(), u["id"]))
    return user_public(db.one("SELECT * FROM users WHERE id=?", (u["id"],)))


# =============================================================== настройки (админ)
@app.get("/api/settings")
async def get_settings(request: Request):
    u = me(request)
    out = {"is_admin": bool(u["is_admin"]), "telegram": {"bot": tgbot.bot_username(), "configured": bool(tgbot.token())},
           "llm": {"configured": llm.configured(), "mock": llm.mock(), "models": llm.forced_models(), "last_model": llm.last_model}}
    if u["is_admin"]:
        out["llm"]["key_hint"] = ("…" + llm.api_key()[-4:]) if llm.api_key() else ""
    return out


@app.put("/api/settings")
async def put_settings(request: Request, body: dict = Body(...)):
    u = me(request)
    if not u["is_admin"]:
        raise HTTPException(403, "Только администратор (первый зарегистрированный пользователь)")
    if "openrouter_key" in body:
        db.set_setting("openrouter_key", str(body["openrouter_key"]).strip())
    if "llm_models" in body:
        db.set_setting("llm_models", str(body["llm_models"]).strip())
        llm._models = []
    if "telegram_token" in body:
        db.set_setting("telegram_token", str(body["telegram_token"]).strip())
        await tgbot.restart()
    return await get_settings(request)


@app.post("/api/settings/test")
async def test_llm(request: Request):
    me(request)
    try:
        obj = await llm.chat_json("Отвечай только JSON.", 'Верни JSON {"ok": true, "say": "привет"}', task="ping",
                                  ctx={}, max_tokens=60, temperature=0)
        return {"ok": True, "model": llm.last_model or "mock", "reply": obj}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


# =============================================================== курсы
def course_progress(c: dict) -> dict:
    mins_left, lessons_left, lessons_total = gm.course_minutes_left(c["id"])
    done = max(0, lessons_total - lessons_left)
    pct = 100 if c["status"] == "completed" else min(99, round(done / max(lessons_total, 1) * 100)) if c["status"] == "ready" else 0
    return {"percent": pct, "lessons_done": done, "lessons_total": lessons_total, "minutes_left": mins_left}


def course_card(c: dict, user: dict) -> dict:
    nl = generator.next_lesson(c["id"]) if c["status"] in ("ready", "completed") else None
    return {"id": c["id"], "title": c["title"], "description": c["description"], "icon": c["icon"], "status": c["status"],
            "status_text": c["status_text"], "error": c["error"], "progress": course_progress(c),
            "xp": gm.total_xp(user["id"], c["id"]), "streak": gm.streak_info(user["id"], c["id"]),
            "goal": gm.goal_info(c, user["id"]), "next_title": nl["title"] if nl else None,
            "has_practice": bool(c["has_practice"]), "created_at": c["created_at"]}


@app.get("/api/courses")
async def list_courses(request: Request):
    u = me(request)
    rows = db.q("SELECT * FROM courses WHERE user_id=? ORDER BY COALESCE(last_opened, created_at) DESC", (u["id"],))
    return [course_card(c, u) for c in rows]


@app.post("/api/courses")
async def create_course(request: Request, files: list[UploadFile] = File(...), title: str = Form("")):
    u = me(request)
    blobs = [(f.filename or "file.txt", await f.read()) for f in files]
    cid, warnings = await run_in_threadpool(service.create_course, u["id"], blobs, title)
    return {"id": cid, "warnings": warnings}


@app.get("/api/courses/{cid}")
async def course_detail(cid: int, request: Request):
    u = me(request)
    c = own_course(u, cid)
    db.x("UPDATE courses SET last_opened=CURRENT_TIMESTAMP WHERE id=?", (cid,))
    card = course_card(c, u)
    mods = db.q("SELECT * FROM modules WHERE course_id=? ORDER BY idx", (cid,))
    per = {r["module_id"]: r for r in db.q(
        "SELECT module_id, COUNT(*) n, SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) d, AVG(CASE WHEN status='done' AND type LIKE '%test' THEN score END) acc FROM lessons WHERE course_id=? GROUP BY module_id", (cid,))}
    out, current_set = [], False
    for m in mods:
        r = per.get(m["id"]) or {}
        if m["status"] == "done":
            st = "done"
        elif not current_set:
            st, current_set = "current", True
        else:
            st = "locked"
        plan = jl(m["plan"], {}) or {}
        out.append({"id": m["id"], "idx": m["idx"], "title": m["title"], "summary": m["summary"], "state": st, "kind": m["kind"],
                    "lessons_done": r.get("d") or 0, "lessons_total": r.get("n") or 0, "planned": m["status"] != "new",
                    "terms": [t["term"] for t in plan.get("terms", [])] if st != "locked" else [],
                    "message": plan.get("message"), "accuracy": round(r["acc"] * 100) if r.get("acc") is not None else None})
    card["modules"] = out
    card["sources"] = [{"filename": s["filename"], "chars": s["chars"]} for s in db.q("SELECT filename, chars FROM sources WHERE course_id=?", (cid,))]
    card["notes"] = c["notes"]
    card["week"] = gm.week(u["id"], cid)
    card["lessons_done_total"] = db.val("SELECT COUNT(*) FROM lessons WHERE course_id=? AND status='done'", (cid,), 0)
    return card


@app.delete("/api/courses/{cid}")
async def delete_course(cid: int, request: Request):
    u = me(request)
    own_course(u, cid)
    with db.tx():
        for t in ("sources", "chunks", "modules", "lessons", "answers", "activity"):
            if t == "activity":
                db.x("DELETE FROM activity WHERE course_id=? AND user_id=?", (cid, u["id"]))
            else:
                db.x(f"DELETE FROM {t} WHERE course_id=?", (cid,))
        db.x("DELETE FROM runs WHERE lesson_id NOT IN (SELECT id FROM lessons)")
        db.x("DELETE FROM courses WHERE id=?", (cid,))
    return {"ok": True}


@app.post("/api/courses/{cid}/retry")
async def retry_course(cid: int, request: Request):
    u = me(request)
    c = own_course(u, cid)
    if c["status"] != "error":
        raise HTTPException(400, "Курс не в состоянии ошибки")
    db.x("UPDATE courses SET status='processing', status_text='В очереди…', error=NULL WHERE id=?", (cid,))
    generator.spawn(generator.build_course(cid))
    return {"ok": True}


@app.put("/api/courses/{cid}/goal")
async def set_goal(cid: int, request: Request, body: dict = Body(...)):
    u = me(request)
    c = own_course(u, cid)
    gd = body.get("date")
    if gd:
        try:
            d = date.fromisoformat(gd)
        except ValueError:
            raise HTTPException(400, "Дата в формате ГГГГ-ММ-ДД")
        if d < date.today():
            raise HTTPException(400, "Срок не может быть в прошлом")
        db.x("UPDATE courses SET goal_date=?, goal_set_at=? WHERE id=?", (gd, gm.today(), cid))
    else:
        db.x("UPDATE courses SET goal_date=NULL WHERE id=?", (cid,))
    gm.check_achievements(u["id"])
    return gm.goal_info(db.one("SELECT * FROM courses WHERE id=?", (cid,)), u["id"])


@app.get("/api/courses/{cid}/next")
async def course_next(cid: int, request: Request, retry: int = 0):
    u = me(request)
    c = own_course(u, cid)
    if c["status"] == "processing":
        return {"state": "processing", "text": c["status_text"]}
    if c["status"] == "error":
        return {"state": "error", "error": c["error"]}
    if c["status"] == "completed":
        return {"state": "completed"}
    l = generator.next_lesson(cid)
    if not l:
        nm = generator.next_unplanned_module(cid)
        if nm:
            err = (jl(nm["plan"], {}) or {}).get("error")
            if err and not retry:
                return {"state": "failed", "error": err}
            if err:
                db.x("UPDATE modules SET plan=NULL WHERE id=?", (nm["id"],))
            if not generator.lock(f"module:{nm['id']}").locked():
                generator.spawn(generator.prepare_next(cid))
            return {"state": "preparing", "text": "Готовлю следующий модуль…"}
        db.x("UPDATE courses SET status='completed' WHERE id=?", (cid,))
        return {"state": "completed"}
    if l["status"] in ("ready",):
        generator.spawn(generator.prefetch(cid))
        return {"state": "ready", "lesson_id": l["id"], "title": l["title"], "type": l["type"], "minutes": l["minutes"]}
    if l["status"] == "failed" and not retry:
        return {"state": "failed", "error": l["error"], "lesson_id": l["id"]}
    if l["status"] in ("pending", "failed"):
        if l["status"] == "failed":
            db.x("UPDATE lessons SET status='pending' WHERE id=?", (l["id"],))
        generator.spawn(generator.generate_lesson(l["id"]))
    return {"state": "generating", "lesson_id": l["id"], "title": l["title"], "type": l["type"]}


# =============================================================== уроки и запуски
def _own_lesson(user: dict, lesson_id: int) -> dict:
    l = db.one("SELECT l.* FROM lessons l JOIN courses c ON c.id=l.course_id WHERE l.id=? AND c.user_id=?", (lesson_id, user["id"]))
    if not l:
        raise HTTPException(404, "Урок не найден")
    return l


@app.post("/api/lessons/{lid}/start")
async def start_lesson(lid: int, request: Request, body: dict = Body(default={})):
    u = me(request)
    l = _own_lesson(u, lid)
    if l["status"] not in ("ready", "done"):
        raise HTTPException(409, "Урок ещё готовится")
    run = engine.start_run(u["id"], l, restart=bool(body.get("restart")))
    generator.spawn(generator.prefetch(l["course_id"]))
    run, lesson, state = engine.get_run(u["id"], run["id"])
    return engine.run_view(run, lesson, state)


@app.get("/api/runs/{rid}")
async def get_run(rid: int, request: Request):
    u = me(request)
    run, lesson, state = engine.get_run(u["id"], rid)
    v = engine.run_view(run, lesson, state)
    if run["finished_at"]:
        v["summary"] = state.get("summary")
    return v


@app.post("/api/runs/{rid}/answer")
async def answer(rid: int, request: Request, body: dict = Body(...)):
    u = me(request)
    res = await engine.submit(u["id"], rid, body)
    run, lesson, state = engine.get_run(u["id"], rid)
    res["next"] = engine.run_view(run, lesson, state)
    return res


@app.post("/api/runs/{rid}/override")
async def override(rid: int, request: Request):
    u = me(request)
    return engine.override(u["id"], rid)


@app.post("/api/runs/{rid}/finish")
async def finish(rid: int, request: Request):
    u = me(request)
    run, lesson, state = engine.get_run(u["id"], rid)
    first = not run["finished_at"]
    summary = engine.finish_run(u["id"], rid)
    if first:
        generator.spawn(generator.after_lesson(lesson["id"]))
    c = db.one("SELECT * FROM courses WHERE id=?", (lesson["course_id"],))
    return {"summary": summary, "course": course_card(c, u)}


# =============================================================== статистика
@app.get("/api/stats")
async def stats(request: Request, course_id: int | None = None):
    u = me(request)
    if course_id:
        own_course(u, course_id)
    return {
        "level": gm.level_info(gm.total_xp(u["id"])),
        "streak": gm.streak_info(u["id"], course_id),
        "heatmap": gm.heatmap(u["id"], course_id),
        "week": gm.week(u["id"], course_id),
        "achievements": gm.achievements_list(u["id"]),
        "totals": {
            "lessons": db.val("SELECT SUM(lessons) FROM activity WHERE user_id=?" + (" AND course_id=?" if course_id else ""),
                              (u["id"], course_id) if course_id else (u["id"],), 0) or 0,
            "minutes": round((db.val("SELECT SUM(seconds) FROM activity WHERE user_id=?" + (" AND course_id=?" if course_id else ""),
                                     (u["id"], course_id) if course_id else (u["id"],), 0) or 0) / 60),
            "xp": gm.total_xp(u["id"], course_id),
            "answers": db.val("SELECT SUM(answers) FROM activity WHERE user_id=?" + (" AND course_id=?" if course_id else ""),
                              (u["id"], course_id) if course_id else (u["id"],), 0) or 0,
        },
        "courses": [{"id": c["id"], "title": c["title"], "icon": c["icon"], "xp": gm.total_xp(u["id"], c["id"])}
                    for c in db.q("SELECT id, title, icon FROM courses WHERE user_id=?", (u["id"],))],
    }


@app.get("/api/reminder")
async def reminder(request: Request):
    u = me(request)
    return notifier.web_reminder(u)


# =============================================================== Telegram
@app.post("/api/telegram/link")
async def tg_link(request: Request):
    u = me(request)
    if not tgbot.token():
        raise HTTPException(400, "Токен бота не задан. Администратор добавляет его в настройках.")
    code = secrets.token_hex(4).upper()
    db.x("UPDATE users SET tg_link_code=? WHERE id=?", (code, u["id"]))
    bot = tgbot.bot_username()
    return {"code": code, "bot": bot, "url": f"https://t.me/{bot}?start={code}" if bot else None}


@app.post("/api/telegram/unlink")
async def tg_unlink(request: Request):
    u = me(request)
    db.x("UPDATE users SET tg_chat_id=NULL, tg_link_code=NULL, tg_run_id=NULL WHERE id=?", (u["id"],))
    return {"ok": True}


@app.get("/api/telegram/status")
async def tg_status(request: Request):
    u = db.one("SELECT tg_chat_id FROM users WHERE id=?", (me(request)["id"],))
    return {"linked": bool(u["tg_chat_id"]), "bot": tgbot.bot_username(), "configured": bool(tgbot.token()), "error": tgbot.last_error()}


@app.get("/api/health")
async def health():
    return {"ok": True, "llm": llm.configured(), "telegram": bool(tgbot.token())}


# =============================================================== статика
app.mount("/static", StaticFiles(directory=str(config.STATIC)), name="static")


@app.get("/")
async def index():
    return FileResponse(config.STATIC / "index.html", headers={"Cache-Control": "no-cache"})
