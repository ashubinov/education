"""FastAPI-приложение LearnQuest (бэкенд). Фронтенд — отдельный репозиторий (education-front, GitHub Pages)."""
import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from datetime import date

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import auth, catalog, config, engine, generator, notifier, service, supplement, tgbot
from . import gamification as gm
from .db import db, jd, jl
from .llm import llm
from .schemas import AnswerIn, GoalIn, LoginIn, MeUpdate, PasswordChangeIn, PublishIn, RegisterIn, SettingsUpdate, StartIn

MAX_REQUEST_MB = 80


@asynccontextmanager
async def lifespan(app: FastAPI):
    generator.set_loop(asyncio.get_running_loop())
    catalog.system_user_id()
    # восстановление после перезапуска
    db.x("UPDATE lessons SET status='pending' WHERE status='generating'")
    for c in db.q("SELECT id FROM courses WHERE status='processing'"):
        if db.val("SELECT COUNT(*) FROM modules WHERE course_id=?", (c["id"],), 0):
            db.x("UPDATE courses SET status='ready', status_text='' WHERE id=?", (c["id"],))  # прервалось дополнение курса
        else:
            generator.spawn(generator.build_course(c["id"]))
    # готовые курсы: если каталог пуст и рядом лежит catalog_seed.json — загружаем (удобно для первого запуска в облаке)
    removed = catalog.dedupe_templates()  # одинаковые курсы в каталоге не нужны: остаётся с меньшим номером
    if removed:
        print("Из каталога удалены дубликаты, номера:", removed)
    seed = config.DATA / "catalog_seed.json"
    if seed.exists() and not db.val("SELECT COUNT(*) FROM courses WHERE is_template=1", default=0):
        try:
            res = catalog.import_all(json.loads(seed.read_text(encoding="utf-8")))
            print("Каталог загружен из catalog_seed.json:", res)
        except Exception as e:
            print("Не удалось загрузить catalog_seed.json:", e)
    tgbot.start()
    notifier.start()
    yield
    await tgbot.stop()
    await notifier.stop()


app = FastAPI(title="LearnQuest API", lifespan=lifespan, docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")

# ---------- CORS: фронт на GitHub Pages ходит на этот бэкенд с другого домена; авторизация — Bearer-токеном, куки не нужны ----------
_local = [f"http://127.0.0.1:{config.PORT}", f"http://localhost:{config.PORT}"]
_origins = [o.strip().rstrip("/") for o in config.env("CORS_ORIGINS").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_origins + _local, allow_credentials=False,
                   allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], allow_headers=["Authorization", "Content-Type"], max_age=600)


@app.middleware("http")
async def security(request: Request, call_next):
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_REQUEST_MB * 1024 * 1024:
        return JSONResponse({"detail": f"Запрос больше {MAX_REQUEST_MB} МБ"}, status_code=413)
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    if not request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.exception_handler(RequestValidationError)
async def validation_error(_, exc: RequestValidationError):
    """Понятное сообщение вместо технического 422."""
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(x) for x in first.get("loc", []) if x not in ("body", "query", "path"))
    msg = first.get("msg", "неверные данные")
    return JSONResponse({"detail": f"Некорректные данные{' (' + field + ')' if field else ''}: {msg}"}, status_code=422)


@app.exception_handler(ValueError)
async def value_error(_, exc: ValueError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(KeyError)
async def key_error(_, exc: KeyError):
    return JSONResponse({"detail": "Не найдено"}, status_code=404)


def me(request: Request) -> dict:
    return auth.require_user(request)


def admin(request: Request) -> dict:
    u = auth.require_user(request)
    if not u["is_admin"]:
        raise HTTPException(403, "Только для администратора")
    return u


def own_course(user: dict, course_id: int) -> dict:
    c = db.one("SELECT * FROM courses WHERE id=? AND user_id=?", (course_id, user["id"]))
    if not c:
        raise HTTPException(404, "Курс не найден")
    return c


# =============================================================== профиль и вход
def user_public(u: dict) -> dict:
    xp = gm.total_xp(u["id"])
    return {
        "id": u["id"], "username": u["username"], "display_name": u["display_name"] or u["username"], "avatar": u["avatar"],
        "theme_color": u["theme_color"], "theme_mode": u["theme_mode"], "sound": bool(u["sound"]),
        "reminders_on": bool(u["reminders_on"]), "reminder_time": u["reminder_time"], "is_admin": bool(u["is_admin"]),
        "telegram": bool(u["tg_chat_id"]), "level": gm.level_info(xp), "streak": gm.streak_info(u["id"]),
    }


def _session(uid: int) -> dict:
    return {"token": auth.make_token(uid), "user": user_public(db.one("SELECT * FROM users WHERE id=?", (uid,)))}


@app.post("/api/auth/register")
async def register(request: Request, body: RegisterIn):
    uid = await run_in_threadpool(auth.register, body.username, body.password, body.display_name, auth.client_ip(request))
    return _session(uid)


@app.post("/api/auth/login")
async def login(request: Request, body: LoginIn):
    uid = await run_in_threadpool(auth.login, body.username, body.password, auth.client_ip(request))
    return _session(uid)


@app.post("/api/auth/logout-all")
async def logout_all(request: Request):
    """Выйти на всех устройствах: все выданные токены перестают действовать."""
    auth.revoke_all(me(request)["id"])
    return {"ok": True}


@app.post("/api/me/password")
async def change_password(request: Request, body: PasswordChangeIn):
    """Смена пароля. Другие устройства выходят из аккаунта; текущему возвращается свежий токен."""
    u = me(request)
    await run_in_threadpool(auth.change_password, u["id"], body.current_password, body.new_password, auth.client_ip(request))
    return {"token": auth.make_token(u["id"]), "ok": True}


@app.get("/api/me")
async def get_me(request: Request):
    return user_public(me(request))


@app.put("/api/me")
async def update_me(request: Request, body: MeUpdate):
    u = me(request)
    fields = body.model_dump(exclude_none=True)
    for k in ("sound", "reminders_on"):
        if k in fields:
            fields[k] = int(fields[k])
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        db.x(f"UPDATE users SET {sets} WHERE id=?", (*fields.values(), u["id"]))
    return user_public(db.one("SELECT * FROM users WHERE id=?", (u["id"],)))


# =============================================================== настройки (админ)
def _hint(key: str) -> str:
    return ("…" + key[-4:]) if key else ""


@app.get("/api/settings")
async def get_settings(request: Request):
    u = me(request)
    cu, ds = llm.custom(), llm.deepseek()
    out = {"is_admin": bool(u["is_admin"]), "telegram": {"bot": tgbot.bot_username(), "configured": bool(tgbot.token())},
           "llm": {"configured": llm.configured(), "mock": llm.mock(), "last_model": llm.last_model,
                   "openrouter": bool(llm.api_key()), "deepseek": bool(ds), "paid": llm.paid_allowed(), "models": llm.forced_models(),
                   "custom": {"base": cu["base"], "models": cu["models"], "key_set": bool(cu["key"])} if cu else None}}
    if u["is_admin"]:
        out["llm"]["key_hint"] = _hint(llm.api_key())
        out["llm"]["deepseek_hint"] = _hint(ds["key"]) if ds else ""
        try:
            names = {"deepseek": "DeepSeek", "custom": "свой API", "openrouter": "OpenRouter"}
            out["llm"]["chain"] = [f"{m} ({names[p]})" for p, m in await llm.chain()][:8]
        except Exception:
            out["llm"]["chain"] = []
    return out


@app.put("/api/settings")
async def put_settings(request: Request, body: SettingsUpdate):
    admin(request)
    data = body.model_dump(exclude_none=True)
    for key in ("openrouter_key", "deepseek_key", "llm_base_url", "llm_api_key", "llm_custom_models", "llm_models"):
        if key in data:
            db.set_setting(key, data[key].strip())
    if "llm_paid" in data:
        db.set_setting("llm_paid", "1" if data["llm_paid"] else "0")
    if any(k in data for k in ("llm_models", "llm_paid", "openrouter_key", "deepseek_key", "llm_base_url", "llm_custom_models")):
        llm.reset_cache()
    if "telegram_token" in data:
        db.set_setting("telegram_token", data["telegram_token"].strip())
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
    origin_no = db.val("SELECT catalog_no FROM courses WHERE id=?", (c["origin_id"],)) if c.get("origin_id") else None
    return {"id": c["id"], "title": c["title"], "description": c["description"], "icon": c["icon"], "status": c["status"],
            "status_text": c["status_text"], "error": c["error"] if c["status"] != "ready" else None, "progress": course_progress(c),
            "xp": gm.total_xp(user["id"], c["id"]), "streak": gm.streak_info(user["id"], c["id"]),
            "goal": gm.goal_info(c, user["id"]), "next_title": nl["title"] if nl else None,
            "has_practice": bool(c["has_practice"]), "created_at": c["created_at"], "catalog_no": origin_no}


@app.get("/api/courses")
async def list_courses(request: Request):
    u = me(request)
    rows = db.q("SELECT * FROM courses WHERE user_id=? AND is_template=0 ORDER BY COALESCE(last_opened, created_at) DESC", (u["id"],))
    return [course_card(c, u) for c in rows]


@app.post("/api/courses")
async def create_course(request: Request, files: list[UploadFile] = File(..., max_length=20), title: str = Form("", max_length=100)):
    u = me(request)
    blobs = [(f.filename or "file.txt", await f.read()) for f in files]
    cid, warnings = await run_in_threadpool(service.create_course, u["id"], blobs, title)
    return {"id": cid, "warnings": warnings}


@app.post("/api/courses/{cid}/supplement")
async def supplement_endpoint(cid: int, request: Request, files: list[UploadFile] = File(..., max_length=20)):
    """Дополнить курс новыми материалами: по ним ИИ добавит новые модули в конец."""
    u = me(request)
    c = own_course(u, cid)
    if c["status"] not in ("ready", "completed"):
        raise HTTPException(409, "Курс сейчас обрабатывается — дождитесь окончания")
    blobs = [(f.filename or "file.txt", await f.read()) for f in files]
    ids, warnings = await run_in_threadpool(service.add_sources, u["id"], cid, blobs)
    generator.spawn(supplement.supplement_course(cid, ids))
    return {"ok": True, "warnings": warnings}


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
    if c["status"] == "ready" and c["error"]:  # ошибка неудавшегося дополнения показывается один раз
        card["supplement_error"] = c["error"]
        db.x("UPDATE courses SET error=NULL WHERE id=?", (cid,))
    return card


@app.delete("/api/courses/{cid}")
async def delete_course(cid: int, request: Request):
    u = me(request)
    own_course(u, cid)
    catalog.delete_course(cid)
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


@app.post("/api/courses/{cid}/restart")
async def restart_course(cid: int, request: Request):
    """Повторение курса: прогресс сбрасывается, XP/серия/достижения остаются."""
    u = me(request)
    c = own_course(u, cid)
    if c["status"] not in ("ready", "completed"):
        raise HTTPException(409, "Курс сейчас обрабатывается")
    catalog.restart_course(cid)
    return {"ok": True}


@app.put("/api/courses/{cid}/goal")
async def set_goal(cid: int, request: Request, body: GoalIn):
    u = me(request)
    own_course(u, cid)
    if body.date:
        if body.date < date.today():
            raise HTTPException(400, "Срок не может быть в прошлом")
        db.x("UPDATE courses SET goal_date=?, goal_set_at=? WHERE id=?", (body.date.isoformat(), gm.today(), cid))
    else:
        db.x("UPDATE courses SET goal_date=NULL WHERE id=?", (cid,))
    gm.check_achievements(u["id"])
    return gm.goal_info(db.one("SELECT * FROM courses WHERE id=?", (cid,)), u["id"])


@app.post("/api/courses/{cid}/publish")
async def publish_course(cid: int, request: Request, body: PublishIn = Body(default=PublishIn())):
    """Администратор: сделать копию курса общедоступной в каталоге (получит номер)."""
    u = admin(request)
    c = own_course(u, cid)
    if c["status"] not in ("ready", "completed"):
        raise HTTPException(409, "Курс ещё не готов")
    return {"number": await run_in_threadpool(catalog.publish, cid, body.title)}


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


# =============================================================== каталог готовых курсов
@app.get("/api/catalog")
async def catalog_list(request: Request):
    u = me(request)
    return catalog.list_catalog(u["id"])


@app.get("/api/catalog/{number}")
async def catalog_get(number: int, request: Request):
    u = me(request)
    c = catalog.find(number)
    if not c:
        raise HTTPException(404, f"Курс с номером {number} не найден")
    return catalog._card(c, u["id"])


@app.post("/api/catalog/{number}/add")
async def catalog_add(number: int, request: Request):
    u = me(request)
    res = await run_in_threadpool(catalog.add_to_user, number, u["id"])
    gm.check_achievements(u["id"])
    return res


@app.delete("/api/admin/catalog/{number}")
async def catalog_delete(number: int, request: Request):
    """Администратор: удалить курс из каталога (копии у пользователей остаются)."""
    admin(request)
    await run_in_threadpool(catalog.delete_template, number)
    return {"ok": True, "number": number}


@app.get("/api/admin/catalog/export")
async def catalog_export(request: Request):
    admin(request)
    data = await run_in_threadpool(catalog.export_all)
    return Response(json.dumps(data, ensure_ascii=False), media_type="application/json",
                    headers={"Content-Disposition": "attachment; filename=catalog_seed.json"})


@app.post("/api/admin/catalog/import")
async def catalog_import(request: Request, file: UploadFile = File(...), replace: int = 0):
    """Импорт каталога из файла экспорта. replace=1 — заменить курсы с совпадающими номерами."""
    admin(request)
    try:
        data = json.loads((await file.read()).decode("utf-8"))
    except Exception:
        raise HTTPException(400, "Это не JSON-файл каталога")
    return await run_in_threadpool(catalog.import_all, data, bool(replace))


# =============================================================== уроки и запуски
def _own_lesson(user: dict, lesson_id: int) -> dict:
    l = db.one("SELECT l.* FROM lessons l JOIN courses c ON c.id=l.course_id WHERE l.id=? AND c.user_id=?", (lesson_id, user["id"]))
    if not l:
        raise HTTPException(404, "Урок не найден")
    return l


@app.post("/api/lessons/{lid}/start")
async def start_lesson(lid: int, request: Request, body: StartIn = Body(default=StartIn())):
    u = me(request)
    l = _own_lesson(u, lid)
    if l["status"] not in ("ready", "done"):
        raise HTTPException(409, "Урок ещё готовится")
    run = engine.start_run(u["id"], l, restart=body.restart)
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
async def answer(rid: int, request: Request, body: AnswerIn):
    u = me(request)
    res = await engine.submit(u["id"], rid, body.to_payload())
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
    w = " AND course_id=?" if course_id else ""
    a = (u["id"], course_id) if course_id else (u["id"],)
    return {
        "level": gm.level_info(gm.total_xp(u["id"])),
        "streak": gm.streak_info(u["id"], course_id),
        "heatmap": gm.heatmap(u["id"], course_id),
        "week": gm.week(u["id"], course_id),
        "achievements": gm.achievements_list(u["id"]),
        "totals": {
            "lessons": db.val("SELECT SUM(lessons) FROM activity WHERE user_id=?" + w, a, 0) or 0,
            "minutes": round((db.val("SELECT SUM(seconds) FROM activity WHERE user_id=?" + w, a, 0) or 0) / 60),
            "xp": gm.total_xp(u["id"], course_id),
            "answers": db.val("SELECT SUM(answers) FROM activity WHERE user_id=?" + w, a, 0) or 0,
        },
        "courses": [{"id": c["id"], "title": c["title"], "icon": c["icon"], "xp": gm.total_xp(u["id"], c["id"])}
                    for c in db.q("SELECT id, title, icon FROM courses WHERE user_id=? AND is_template=0", (u["id"],))],
    }


@app.post("/api/prank/yes")
async def prank_yes(request: Request):
    """Розыгрыш: нажатие «ДА» в окне после регистрации выдаёт скрытое достижение."""
    u = me(request)
    a = gm.ACH_BY_KEY["sosun"]
    fresh = db.one("SELECT 1 AS x FROM achievements WHERE user_id=? AND key='sosun'", (u["id"],)) is None
    db.x("INSERT OR IGNORE INTO achievements(user_id,key) VALUES(?,?)", (u["id"], "sosun"))
    return {"new": fresh, "achievement": {"key": a[0], "icon": a[1], "title": a[2], "desc": a[3]}}


@app.get("/api/reminder")
async def reminder(request: Request):
    return notifier.web_reminder(me(request))


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


# =============================================================== фронтенд (только для локального запуска, если рядом лежит папка front/)
_front = config.FRONT_DIR
if (_front / "index.html").exists():
    @app.get("/config.js")
    async def local_config():
        # на локальном запуске фронт и бэкенд на одном адресе — префикс API не нужен
        return PlainTextResponse('window.LQ_API = "";\n', media_type="application/javascript", headers={"Cache-Control": "no-cache"})

    app.mount("/", StaticFiles(directory=str(_front), html=True), name="front")
else:
    @app.get("/")
    async def root():
        return {"service": "LearnQuest API", "docs": "/api/docs", "health": "/api/health"}
