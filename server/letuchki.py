"""Летучки — короткие контрольные по лекциям (сейчас только «Операционные системы»), которые лектор раздаёт на занятиях.

Структура: предмет → летучка (тема) → вариант (1 или 2) → два вопроса. Ответ пишется текстом и сравнивается с эталоном из файла лектора
локально (без нейросети, см. `grading_score`). Летучки не закрыты и не подстраиваются под ученика: можно решать любую и сколько угодно раз.
Опыт (XP) начисляется только за ПЕРВОЕ прорешивание варианта.

Материал загружается админом из markdown-файла такого вида (в нём можно дополнять летучки новыми лекциями, повторная загрузка обновляет
совпавшие летучки и варианты и не трогает остальные; результаты учеников сохраняются):

    ## Летучка 1. Название темы
    ### Вариант 1
    **Вопрос 1 (часть 1). Текст вопроса.**
    эталонный ответ …
    **Вопрос 2 (часть 2). ✏️ Нарисуйте схему … (значок ✏️ — вопрос по рисунку: ученик описывает схему словами)**
    эталонный ответ …
    ### Вариант 2
    …
"""
from __future__ import annotations

import re
import time

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from . import auth, grading
from . import gamification as gm
from .db import db

router = APIRouter(prefix="/api/letuchki", tags=["Летучки"])
DEFAULT_SUBJECT = "Операционные системы"
XP_CORRECT, XP_PARTIAL = 15, 7      # опыт за вопрос, только за первое прорешивание
OK_SCORE, PARTIAL_SCORE = 0.55, 0.22
MAX_ANSWER = 6000
PASS_COOLDOWN = 30   # один и тот же вариант засчитывается в достижения не чаще, чем раз в 30 секунд


def init_schema():
    with db.lock:
        db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS lt_subjects (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL UNIQUE);
        CREATE TABLE IF NOT EXISTS lt_sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_id INTEGER NOT NULL,
            topic_no INTEGER NOT NULL,
            topic_title TEXT NOT NULL,
            variant INTEGER NOT NULL,
            UNIQUE(subject_id, topic_no, variant)
        );
        CREATE TABLE IF NOT EXISTS lt_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            set_id INTEGER NOT NULL,
            idx INTEGER NOT NULL,
            title TEXT NOT NULL,
            answer TEXT NOT NULL,
            draw INTEGER NOT NULL DEFAULT 0,
            UNIQUE(set_id, idx)
        );
        CREATE TABLE IF NOT EXISTS lt_results (
            user_id INTEGER NOT NULL,
            set_id INTEGER NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            first_overall TEXT,
            best_overall TEXT,
            xp INTEGER NOT NULL DEFAULT 0,
            last_ts INTEGER,
            PRIMARY KEY(user_id, set_id)
        );
        """)
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(lt_results)")}
        for col in ("passed", "last_pass_ts"):   # засчитанные прохождения (для достижений) и время последнего засчитанного
            if col not in cols:
                db.conn.execute(f"ALTER TABLE lt_results ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
        db.conn.commit()


# ------------------------------------------------------------------ разбор файла лектора
_TOPIC = re.compile(r"^##\s+Летучка\s+(\d+)\.?\s*(.*?)\s*$", re.I)
_VARIANT = re.compile(r"^###\s+Вариант\s+(\d+)", re.I)
_QUESTION = re.compile(r"^\*\*\s*Вопрос\s+(\d+)\s*(?:\([^)]*\))?\s*\.?\s*(.*?)\*\*\s*$", re.I)
_EMBED = re.compile(r"^\s*(?:&#91;|\[)\s*embedded content.*$", re.I)


def parse_md(text: str) -> list[dict]:
    """Файл → список вариантов: {topic_no, topic_title, variant, questions: [{title, answer, draw}]}."""
    out, topic, variant, cur = [], None, None, None
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        m = _TOPIC.match(line)
        if m:
            topic, variant, cur = (int(m.group(1)), m.group(2).strip()), None, None
            continue
        m = _VARIANT.match(line)
        if m and topic:
            variant = {"topic_no": topic[0], "topic_title": topic[1], "variant": int(m.group(1)), "questions": []}
            out.append(variant)
            cur = None
            continue
        m = _QUESTION.match(line.strip())
        if m and variant is not None:
            title = m.group(2).strip()
            draw = "✏" in title
            title = re.sub(r"✏️?\s*", "", title).strip()
            cur = {"title": title, "lines": [], "draw": int(draw)}
            variant["questions"].append(cur)
            continue
        if line.startswith("## ") and not _TOPIC.match(line):
            variant = cur = None    # другой раздел файла (не летучка)
            continue
        if cur is not None and not _EMBED.match(line) and line.strip() != "---":
            cur["lines"].append(line)
    res = []
    for v in out:
        qs = []
        for q in v["questions"]:
            ans = re.sub(r"\n{3,}", "\n\n", "\n".join(q["lines"]).strip())
            if q["title"] and ans:
                qs.append({"title": q["title"], "answer": ans, "draw": q["draw"]})
        if qs:
            res.append({**{k: v[k] for k in ("topic_no", "topic_title", "variant")}, "questions": qs})
    return res


def import_md(text: str, subject: str = DEFAULT_SUBJECT) -> dict:
    """Добавить/обновить летучки из файла. Совпавшие (предмет, тема, вариант) обновляются, остальные не затрагиваются."""
    items = parse_md(text)
    if not items:
        raise ValueError("В файле нет летучек: ожидаются заголовки «## Летучка N. …», «### Вариант K» и «**Вопрос M …**»")
    subject = (subject or DEFAULT_SUBJECT).strip()[:100]
    added = updated = 0
    with db.tx():
        sid = db.val("SELECT id FROM lt_subjects WHERE title=?", (subject,), None) or db.x("INSERT INTO lt_subjects(title) VALUES(?)", (subject,))
        for it in items:
            row = db.one("SELECT id FROM lt_sets WHERE subject_id=? AND topic_no=? AND variant=?", (sid, it["topic_no"], it["variant"]))
            if row:
                db.x("UPDATE lt_sets SET topic_title=? WHERE id=?", (it["topic_title"], row["id"]))
                db.x("DELETE FROM lt_questions WHERE set_id=?", (row["id"],))
                set_id, updated = row["id"], updated + 1
            else:
                set_id = db.x("INSERT INTO lt_sets(subject_id,topic_no,topic_title,variant) VALUES(?,?,?,?)", (sid, it["topic_no"], it["topic_title"], it["variant"]))
                added += 1
            for i, q in enumerate(it["questions"]):
                db.x("INSERT INTO lt_questions(set_id,idx,title,answer,draw) VALUES(?,?,?,?,?)", (set_id, i, q["title"], q["answer"], q["draw"]))
    return {"subject": subject, "subject_id": sid, "added": added, "updated": updated, "topics": len({i["topic_no"] for i in items})}


# ------------------------------------------------------------------ проверка ответа
def _clean(md: str) -> str:
    s = re.sub(r"\(\?\)", " ", md or "")
    s = re.sub(r"[*_`#>|]", " ", s)
    s = re.sub(r"^\s*(?:[-•]|\d+[.)])\s+", "", s, flags=re.M)
    return s


def _points(md: str) -> list[set[str]]:
    """Эталон → список «смысловых пунктов» (строки списков и предложения), каждый — набор основ слов."""
    pts = []
    for part in re.split(r"\n+|(?<=[.;!?])\s+", _clean(md)):
        st = grading.stems(part)
        if len(st) >= 2:
            pts.append(st)
    return pts


def grading_score(answer: str, reference: str) -> float:
    """Насколько ответ ученика близок к эталону (0…1): доля слов эталона, которые есть в ответе, и доля пунктов эталона, покрытых ответом."""
    ans = (answer or "").strip()
    a = grading.stems(ans)
    if len(ans) < 12 or len(a) < 2:
        return 0.0
    ref = grading.stems(_clean(reference))
    if not ref:
        return 0.0
    cov = len(ref & a) / len(ref)
    pts = _points(reference)
    hit = sum(1 for p in pts if len(p & a) / len(p) >= 0.5) / len(pts) if pts else 0.0
    return min(1.0, max(cov * 1.35, hit * 0.9 + cov * 0.2))


def verdict_of(score: float) -> str:
    return "correct" if score >= OK_SCORE else "partial" if score >= PARTIAL_SCORE else "wrong"


def overall_of(verdicts: list[str]) -> str:
    """ok — все ответы верны (дрон взрывается), fail — ни одного верного (видео «неверно»), partial — остальное (дрон улетает)."""
    if all(v == "correct" for v in verdicts):
        return "ok"
    if all(v == "wrong" for v in verdicts):
        return "fail"
    return "partial"


_RANK = {"fail": 0, "partial": 1, "ok": 2}


def check(user_id: int, set_id: int, answers: list[str]) -> dict:
    s = db.one("SELECT * FROM lt_sets WHERE id=?", (set_id,))
    if not s:
        raise KeyError("set")
    qs = db.q("SELECT idx, title, answer, draw FROM lt_questions WHERE set_id=? ORDER BY idx", (set_id,))
    if len(answers) != len(qs):
        raise ValueError(f"Нужно ответить на все вопросы ({len(qs)})")
    items, xp = [], 0
    for q, a in zip(qs, answers):
        sc = grading_score(a, q["answer"])
        v = verdict_of(sc)
        items.append({"verdict": v, "score": round(sc, 2), "reference": q["answer"]})
    overall = overall_of([i["verdict"] for i in items])
    now = int(time.time())
    with db.tx():
        row = db.one("SELECT * FROM lt_results WHERE user_id=? AND set_id=?", (user_id, set_id))
        first = row is None
        # засчитанное прохождение (идёт в достижения, повторные тоже): не «неверно» и не чаще раза в PASS_COOLDOWN секунд на вариант
        counted = overall != "fail" and (first or now - (row["last_pass_ts"] or 0) >= PASS_COOLDOWN)
        if first:
            xp = sum(XP_CORRECT if i["verdict"] == "correct" else XP_PARTIAL if i["verdict"] == "partial" else 0 for i in items)
            db.x("INSERT INTO lt_results(user_id,set_id,attempts,first_overall,best_overall,xp,last_ts,passed,last_pass_ts) VALUES(?,?,1,?,?,?,?,?,?)",
                 (user_id, set_id, overall, overall, xp, now, int(counted), now if counted else 0))
            gm.record_activity(user_id, 0, xp=xp, answers=len(items), correct=sum(i["score"] if i["verdict"] != "wrong" else 0 for i in items))
        else:
            best = overall if _RANK[overall] > _RANK.get(row["best_overall"], 0) else row["best_overall"]
            db.x("UPDATE lt_results SET attempts=attempts+1, best_overall=?, last_ts=?, passed=passed+?, last_pass_ts=? WHERE user_id=? AND set_id=?",
                 (best, now, int(counted), now if counted else row["last_pass_ts"], user_id, set_id))
        passed_total = db.val("SELECT SUM(passed) FROM lt_results WHERE user_id=?", (user_id,), 0) or 0
    new_ach = gm.check_achievements(user_id) if counted else []
    nxt = next_set(set_id)
    return {"overall": overall, "items": items, "first": first, "xp": xp, "next": nxt, "subject_id": s["subject_id"],
            "attempts": (row["attempts"] if row else 0) + 1, "counted": counted, "passed_total": passed_total, "achievements": new_ach,
            "next_goal": next((n for n, *_ in gm.LT_STEPS if n > passed_total), None)}


def next_set(set_id: int) -> dict | None:
    """Следующий вариант в порядке «тема, вариант» в том же предмете."""
    s = db.one("SELECT * FROM lt_sets WHERE id=?", (set_id,))
    n = db.one("""SELECT id, topic_no, topic_title, variant FROM lt_sets WHERE subject_id=? AND (topic_no>? OR (topic_no=? AND variant>?))
                  ORDER BY topic_no, variant LIMIT 1""", (s["subject_id"], s["topic_no"], s["topic_no"], s["variant"]))
    return n


# ------------------------------------------------------------------ API
class CheckIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    answers: list[str] = Field(min_length=1, max_length=10)


@router.get("/subjects")
async def subjects(request: Request):
    u = auth.require_user(request)

    def work():
        rows = db.q("""SELECT s.id, s.title, COUNT(DISTINCT t.topic_no) AS topics, COUNT(t.id) AS sets FROM lt_subjects s
                       LEFT JOIN lt_sets t ON t.subject_id=s.id GROUP BY s.id ORDER BY s.title""")
        for r in rows:
            r["done"] = db.val("""SELECT COUNT(*) FROM lt_results r JOIN lt_sets t ON t.id=r.set_id WHERE r.user_id=? AND t.subject_id=?""", (u["id"], r["id"]), 0)
        return rows
    return await run_in_threadpool(work)


@router.get("/subjects/{sid}")
async def topics(sid: int, request: Request):
    u = auth.require_user(request)

    def work():
        sub = db.one("SELECT id, title FROM lt_subjects WHERE id=?", (sid,))
        if not sub:
            raise HTTPException(404, "Предмет не найден")
        res = {r["set_id"]: r for r in db.q("SELECT set_id, attempts, best_overall, first_overall, xp FROM lt_results WHERE user_id=?", (u["id"],))}
        topics_ = {}
        for t in db.q("SELECT id, topic_no, topic_title, variant FROM lt_sets WHERE subject_id=? ORDER BY topic_no, variant", (sid,)):
            e = topics_.setdefault(t["topic_no"], {"no": t["topic_no"], "title": t["topic_title"], "variants": []})
            r = res.get(t["id"])
            e["variants"].append({"set_id": t["id"], "variant": t["variant"], "attempts": r["attempts"] if r else 0,
                                  "best": r["best_overall"] if r else None, "xp": r["xp"] if r else 0})
        return {"subject": sub, "topics": list(topics_.values())}
    return await run_in_threadpool(work)


@router.get("/sets/{set_id}")
async def get_set(set_id: int, request: Request):
    u = auth.require_user(request)

    def work():
        s = db.one("""SELECT t.id, t.topic_no, t.topic_title, t.variant, t.subject_id, s.title AS subject FROM lt_sets t
                      JOIN lt_subjects s ON s.id=t.subject_id WHERE t.id=?""", (set_id,))
        if not s:
            raise HTTPException(404, "Летучка не найдена")
        s["questions"] = [{"idx": q["idx"], "title": q["title"], "draw": bool(q["draw"])} for q in
                          db.q("SELECT idx, title, draw FROM lt_questions WHERE set_id=? ORDER BY idx", (set_id,))]
        r = db.one("SELECT attempts, best_overall, xp FROM lt_results WHERE user_id=? AND set_id=?", (u["id"], set_id))
        s["attempts"], s["best"], s["xp"] = (r["attempts"], r["best_overall"], r["xp"]) if r else (0, None, 0)
        s["next"] = next_set(set_id)
        return s
    return await run_in_threadpool(work)


@router.post("/sets/{set_id}/check")
async def check_set(set_id: int, body: CheckIn, request: Request):
    u = auth.require_user(request)
    answers = [a[:MAX_ANSWER] for a in body.answers]
    try:
        res = await run_in_threadpool(check, u["id"], set_id, answers)
    except KeyError:
        raise HTTPException(404, "Летучка не найдена")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return res


@router.post("/admin/upload")
async def upload(request: Request, file: UploadFile = File(...), subject: str = Form(DEFAULT_SUBJECT)):
    u = auth.require_user(request)
    if not u["is_admin"]:
        raise HTTPException(403, "Только для администратора")
    raw = await file.read()
    if len(raw) > 5_000_000:
        raise HTTPException(400, "Файл слишком большой")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "Нужен текстовый файл (.md) в кодировке UTF-8")
    try:
        return await run_in_threadpool(import_md, text, subject)
    except ValueError as e:
        raise HTTPException(400, str(e))
