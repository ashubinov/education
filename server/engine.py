"""Движок прохождения урока: шаги, проверка ответов, опыт. Общий для веб-интерфейса и Telegram."""
import random
import re
import time
from datetime import datetime

from . import gamification as gm
from . import grading, prompts
from .db import db, jd, jl
from .llm import LLMError, llm

XP = {"read": 2, "single": 10, "multi": 14, "image": 12, "fill": 12, "write_tail": 15, "write_full": 20, "task": 40}
RETRY_TYPES = ("terms", "terms_test", "methods_test")
GRADED = ("single", "multi", "image", "fill", "write", "task")


# ----------------------------------------------------------------------- построение шагов
def split_definition(defn: str) -> tuple[str, str]:
    words = defn.split()
    if len(words) < 6:
        k = max(1, len(words) // 2)
    else:
        k = round(len(words) * 0.45)
        # сдвигаемся к ближайшей запятой/границе предложения, если она рядом
        best = None
        for j in range(max(2, k - 3), min(len(words) - 3, k + 4)):
            if words[j - 1].endswith((",", ";", ":", "—", "-", ".")):
                if best is None or abs(j - k) < abs(best - k):
                    best = j
        if best:
            k = best
    head, tail = " ".join(words[:k]), " ".join(words[k:])
    return head, tail


def build_steps(lesson: dict) -> list[dict]:
    content = jl(lesson["content"], {}) or {}
    typ = lesson["type"]
    steps: list[dict] = []

    def q_steps(qs):
        for q in qs:
            s = dict(q)
            s["step"] = q["kind"]
            steps.append(s)

    if typ in ("intro_test", "terms_test", "methods_test"):
        q_steps(content.get("questions", []))
    elif typ == "terms":
        terms = content.get("terms", [])
        for t in terms:
            steps.append({"step": "read", "concept": t["term"], "term": t["term"], "definition": t["definition"], "example": t.get("example", "")})
        for t in terms:
            head, tail = split_definition(t["definition"])
            steps.append({"step": "write", "stage": "tail", "concept": t["term"], "term": t["term"], "head": head,
                          "reference": tail, "full": t["definition"], "key_points": t.get("key_points", []), "example": t.get("example", "")})
        order = list(range(len(terms)))
        if len(order) > 1:
            order = order[1:] + order[:1]  # другой порядок на последнем круге
        for i in order:
            t = terms[i]
            steps.append({"step": "write", "stage": "full", "concept": t["term"], "term": t["term"], "reference": t["definition"],
                          "full": t["definition"], "key_points": t.get("key_points", []), "example": t.get("example", "")})
    elif typ == "practice":
        task = content.get("task", {})
        steps.append({"step": "task", "concept": task.get("title", "Практика"), "task": task})
    elif typ == "final_test":
        q_steps(content.get("questions", []))
        q_steps(content.get("choose", []))
        for t in content.get("write_terms", []):
            steps.append({"step": "write", "stage": "full", "concept": t["term"], "term": t["term"], "reference": t["definition"],
                          "full": t["definition"], "key_points": t.get("key_points", []), "example": t.get("example", "")})
    return steps


def all_steps(lesson: dict, state: dict) -> list[dict]:
    return build_steps(lesson) + state.get("extra", [])


def public_step(step: dict, idx: int, total: int, lesson: dict) -> dict:
    k = step["step"]
    out = {"idx": idx, "total": total, "kind": k, "retry": bool(step.get("retry")), "concept": step.get("concept", "")}
    if k == "read":
        out.update(term=step["term"], definition=step["definition"], example=step.get("example", ""), title="Новый термин")
    elif k in ("single", "multi", "image"):
        out.update(prompt=step["prompt"], options=step["options"], svg=step.get("svg"), multi=(k == "multi"),
                   title={"single": "Выбери ответ", "multi": "Выбери все верные", "image": "Вопрос по картинке"}[k])
    elif k == "fill":
        out.update(prompt=step["prompt"], title="Дополни термин", placeholder="впиши пропущенное слово или фразу")
    elif k == "write":
        if step["stage"] == "tail":
            out.update(term=step["term"], head=step["head"], title="Допиши описание", placeholder="допиши остаток определения…")
        else:
            out.update(term=step["term"], title="Напиши описание целиком", placeholder="напиши определение своими словами…")
        out["stage"] = step["stage"]
    elif k == "task":
        t = step["task"]
        out.update(title=t.get("title", "Практика"), statement=t.get("statement", ""), starter=t.get("starter", ""),
                   hints=t.get("hints", []), language=t.get("language", ""), task_kind=t.get("kind", "problems"),
                   placeholder="напиши решение…")
    return out


# ----------------------------------------------------------------------- запуски
def get_run(user_id: int, run_id: int) -> tuple[dict, dict, dict]:
    run = db.one("SELECT * FROM runs WHERE id=? AND user_id=?", (run_id, user_id))
    if not run:
        raise KeyError("run")
    lesson = db.one("SELECT * FROM lessons WHERE id=?", (run["lesson_id"],))
    return run, lesson, jl(run["state"], {}) or {}


def start_run(user_id: int, lesson: dict, restart: bool = False) -> dict:
    if not restart:
        r = db.one("SELECT * FROM runs WHERE user_id=? AND lesson_id=? AND finished_at IS NULL ORDER BY id DESC", (user_id, lesson["id"]))
        if r:
            return r
    state = {"answers": [], "extra": [], "replay": lesson["status"] == "done", "xp": 0}
    rid = db.x("INSERT INTO runs(user_id,lesson_id,state,last_step_at) VALUES(?,?,?,?)", (user_id, lesson["id"], jd(state), time.time()))
    return db.one("SELECT * FROM runs WHERE id=?", (rid,))


def current_step(run: dict, lesson: dict, state: dict) -> dict | None:
    steps = all_steps(lesson, state)
    i = run["step_idx"]
    if run["finished_at"] or i >= len(steps):
        return None
    return public_step(steps[i], i, len(steps), lesson)


def run_view(run: dict, lesson: dict, state: dict) -> dict:
    step = current_step(run, lesson, state)
    steps = all_steps(lesson, state)
    return {"run_id": run["id"], "course_id": lesson["course_id"], "lesson": {"id": lesson["id"], "type": lesson["type"], "title": lesson["title"], "minutes": lesson["minutes"]},
            "step": step, "total": len(steps), "done": run["step_idx"], "finished": bool(run["finished_at"]),
            "xp": state.get("xp", 0), "waiting_self": bool(state.get("task_wait"))}


# ----------------------------------------------------------------------- проверка
def _answer_text(step: dict) -> str:
    k = step["step"]
    if k in ("single", "image"):
        return step["options"][step["answer"]]
    if k == "multi":
        return "; ".join(step["options"][i] for i in step["answer"])
    if k == "fill":
        return " / ".join(step["accept"][:3])
    if k == "write":
        return step["reference"] if step["stage"] == "tail" else step["full"]
    if k == "task":
        return step["task"].get("solution", "")
    return ""


def _grade_choice(step: dict, payload: dict) -> float:
    k = step["step"]
    if k in ("single", "image"):
        try:
            return 1.0 if int(payload.get("choice")) == step["answer"] else 0.0
        except (TypeError, ValueError):
            return 0.0
    chosen = payload.get("choice") or []
    if not isinstance(chosen, list):
        chosen = [chosen]
    try:
        chosen = {int(x) for x in chosen}
    except (TypeError, ValueError):
        return 0.0
    right = set(step["answer"])
    if chosen == right:
        return 1.0
    if chosen and chosen <= right:
        return 0.5  # выбрано верное, но не всё
    return 0.0


def _xp_for(step: dict, score: float, state: dict) -> int:
    k = step["step"]
    key = f"write_{step.get('stage')}" if k == "write" else k
    base = XP.get(key, 5) * score
    if step.get("retry"):
        base *= 0.5
    if state.get("replay"):
        base *= 0.25
    return int(round(base))


_run_locks: dict[int, "asyncio.Lock"] = {}


async def submit(user_id: int, run_id: int, payload: dict) -> dict:
    """Принять ответ на текущий шаг. Параллельные запросы одного прохождения (двойной клик) выстраиваются в очередь."""
    import asyncio
    lk = _run_locks.setdefault(run_id, asyncio.Lock())
    async with lk:
        return await _submit(user_id, run_id, payload)


async def _submit(user_id: int, run_id: int, payload: dict) -> dict:
    run, lesson, state = get_run(user_id, run_id)
    if run["finished_at"]:
        raise ValueError("Урок уже завершён")
    steps = all_steps(lesson, state)
    i = run["step_idx"]
    if i >= len(steps):
        raise ValueError("Нет текущего шага")
    if payload.get("idx") is not None and int(payload["idx"]) != i:
        raise ValueError("Этот шаг уже пройден — обнови страницу")
    step = steps[i]
    k = step["step"]
    now = time.time()
    seconds = int(min(150, max(1, now - (run["last_step_at"] or now))))
    result: dict = {"kind": k}

    if k == "read":
        score, graded = 1.0, False
        result.update(correct=None, feedback="", explanation="")
        xp = XP["read"] if not state.get("replay") else 0
    elif k in ("single", "multi", "image"):
        score, graded = _grade_choice(step, payload), True
        result.update(correct=score >= 0.99, partial=0 < score < 0.99, explanation=step.get("explanation", ""),
                      correct_answer=_answer_text(step),
                      correct_idx=step["answer"] if isinstance(step["answer"], list) else [step["answer"]])
        xp = _xp_for(step, score, state)
    elif k == "fill":
        ok = grading.grade_fill(payload.get("text", ""), step["accept"])
        score, graded = (1.0 if ok else 0.0), True
        result.update(correct=ok, explanation=step.get("explanation", ""), correct_answer=_answer_text(step))
        xp = _xp_for(step, score, state)
    elif k == "write":
        text = (payload.get("text") or "").strip()
        score, verdict = grading.grade_write(text, step["reference"], step.get("key_points"))
        graded = True
        result.update(correct=verdict == "correct", partial=verdict == "partial", verdict=verdict, correct_answer=_answer_text(step),
                      explanation=("Пример: " + step["example"]) if step.get("example") else "", can_override=verdict != "correct",
                      user_text=text)
        if step["stage"] == "tail":
            result["full"] = step["full"]
        xp = _xp_for(step, score, state)
    elif k == "task":
        if state.get("task_wait"):
            # второй этап: самооценка при недоступной модели
            try:
                score = {0.0: 0.0, 0.5: 0.5, 1.0: 1.0}[float(payload.get("self"))]
            except Exception:
                score = 0.0
            state["task_wait"] = False
            graded = True
            result.update(correct=score >= 0.99, partial=0 < score < 0.99, feedback="Самооценка засчитана.", correct_answer=_answer_text(step))
            xp = _xp_for(step, score, state)
        elif payload.get("skip"):
            score, graded = 0.0, True
            result.update(correct=False, feedback="Задание пропущено. Разбор — ниже.", correct_answer=_answer_text(step))
            xp = 0
        else:
            text = (payload.get("text") or "").strip()
            if len(text) < 3:
                raise ValueError("Напиши решение")
            try:
                obj = await llm.chat_json(prompts.system("ru"), prompts.evaluate_practice(step["task"], text), task="evaluate",
                                          ctx={"task": step["task"], "answer": text}, max_tokens=1200, temperature=0.2)
                score = max(0.0, min(1.0, float(obj.get("score", 0))))
                score = 1.0 if score >= 0.85 else 0.0 if score < 0.15 else score
                result.update(correct=score >= 0.85, partial=0.2 <= score < 0.85, feedback=str(obj.get("feedback", "")),
                              missing=[str(x) for x in (obj.get("missing") or [])][:5], correct_answer=_answer_text(step))
                for cname in (obj.get("concepts") or [])[:4]:
                    db.x("INSERT INTO answers(user_id,course_id,lesson_id,concept,kind,correct) VALUES(?,?,?,?,?,0)",
                         (user_id, lesson["course_id"], lesson["id"], str(cname)[:80], "task"))
                graded = True
                xp = _xp_for(step, score, state)
            except Exception as e:  # модель недоступна — сверка с эталоном вручную
                state["task_wait"] = True
                db.x("UPDATE runs SET state=? WHERE id=?", (jd(state), run_id))
                return {"kind": "task", "self_check": True, "correct_answer": _answer_text(step),
                        "feedback": "Проверка ИИ сейчас недоступна — сравни своё решение с эталоном и оцени себя сам.",
                        "finished": False, "error": str(e)[:160]}
    else:
        raise ValueError("Неизвестный шаг")

    # --- запись результата
    entry = {"i": i, "score": score, "graded": graded, "concept": step.get("concept", ""), "retry": bool(step.get("retry")), "xp": xp, "kind": k}
    if graded:
        aid = db.x("INSERT INTO answers(user_id,course_id,lesson_id,concept,kind,correct) VALUES(?,?,?,?,?,?)",
                   (user_id, lesson["course_id"], lesson["id"], step.get("concept", "")[:80], k, score))
        entry["aid"] = aid
    state.setdefault("answers", []).append(entry)
    state["xp"] = state.get("xp", 0) + xp
    state["last"] = len(state["answers"]) - 1

    # повтор ошибок в конце урока (один раз)
    requeued = False
    if graded and score < 0.5 and not step.get("retry") and lesson["type"] in RETRY_TYPES and len(state.get("extra", [])) < 3:
        redo = dict(step)
        redo["retry"] = True
        state.setdefault("extra", []).append(redo)
        requeued = True
    result["requeued"] = requeued

    gm.record_activity(user_id, lesson["course_id"], xp=xp, seconds=seconds, answers=1 if graded else 0, correct=score if graded else 0)
    result.update(score=score, xp=xp)
    new_idx = i + 1
    total = len(all_steps(lesson, state))
    finished = new_idx >= total
    db.x("UPDATE runs SET step_idx=?, state=?, last_step_at=? WHERE id=?", (new_idx, jd(state), now, run_id))
    result["finished"] = finished  # итоги — отдельным вызовом finish_run (после экрана с разбором)
    result["done"] = new_idx
    result["total"] = total
    return result


def override(user_id: int, run_id: int) -> dict:
    """Ученик засчитывает себе последний письменный ответ (локальная проверка могла быть слишком строгой)."""
    run, lesson, state = get_run(user_id, run_id)
    if run["finished_at"]:
        raise ValueError("Урок уже завершён")
    idx = state.get("last")
    if idx is None:
        raise ValueError("Нечего пересматривать")
    e = state["answers"][idx]
    if e["kind"] != "write" or e["score"] >= 0.99 or e.get("overridden"):
        raise ValueError("Нельзя пересмотреть")
    base = len(build_steps(lesson))
    step = all_steps(lesson, state)[e["i"]]
    new_xp = _xp_for(step, 1.0, state)
    delta = new_xp - e["xp"]
    e.update(score=1.0, xp=new_xp, overridden=True)
    state["xp"] = state.get("xp", 0) + delta
    if e.get("aid"):
        db.x("UPDATE answers SET correct=0.8 WHERE id=?", (e["aid"],))  # самооценка весит чуть меньше
    # убираем ещё не пройденный повтор этого же вопроса
    extra = state.get("extra", [])
    for pos in range(len(extra) - 1, -1, -1):
        if extra[pos].get("concept") == e["concept"] and extra[pos].get("stage") == step.get("stage") and base + pos >= run["step_idx"]:
            extra.pop(pos)
            break
    gm.record_activity(user_id, lesson["course_id"], xp=delta)
    db.x("UPDATE runs SET state=? WHERE id=?", (jd(state), run_id))
    return {"xp": delta, "total": len(all_steps(lesson, state)), "finished": run["step_idx"] >= len(all_steps(lesson, state))}


# ----------------------------------------------------------------------- завершение
def finish_run(user_id: int, run_id: int) -> dict:
    run, lesson, state = get_run(user_id, run_id)
    if run["finished_at"]:
        return state.get("summary", {})
    graded = [a for a in state.get("answers", []) if a.get("graded") and not a.get("retry")]
    score = sum(a["score"] for a in graded) / len(graded) if graded else 1.0
    first_time = lesson["status"] != "done"
    replay = not first_time
    xp_before = gm.total_xp(user_id)
    lvl_before = gm.level_info(xp_before)["level"]
    streak_before = gm.streak_info(user_id)

    bonus = 0
    if not replay:
        bonus = 20 + round(30 * score)
        if score >= 0.999 and len(graded) >= 5:
            bonus += 10
        if lesson["type"] == "practice":
            bonus += 10
        if lesson["type"] == "final_test":
            bonus += 30
    else:
        bonus = 5
    streak_bonus = 0
    if first_time and not streak_before["done_today"]:
        streak_bonus = min(streak_before["current"] + 1, 10) * 2
    gm.record_activity(user_id, lesson["course_id"], xp=bonus + streak_bonus, lessons=1 if first_time else 0)
    if first_time:
        db.x("UPDATE lessons SET status='done', score=?, completed_at=CURRENT_TIMESTAMP WHERE id=?", (score, lesson["id"]))
    extra = {"lesson_now"}
    if lesson["type"] == "final_test" and first_time:
        mod = db.one("SELECT retries FROM modules WHERE id=?", (lesson["module_id"],))
        if mod and mod["retries"] and score >= 0.6:
            extra.add("comeback")
    new_ach = gm.check_achievements(user_id, extra)
    total_xp = gm.total_xp(user_id)
    li = gm.level_info(total_xp)
    streak = gm.streak_info(user_id)
    summary = {
        "score": round(score, 3), "answers": len(graded), "correct": round(sum(a["score"] for a in graded), 1),
        "xp_steps": state.get("xp", 0), "bonus": bonus, "streak_bonus": streak_bonus,
        "xp_total_gained": state.get("xp", 0) + bonus + streak_bonus,
        "level": li, "level_up": li["level"] > lvl_before, "streak": streak, "achievements": new_ach,
        "replay": replay, "lesson_type": lesson["type"], "lesson_title": lesson["title"],
    }
    state["summary"] = summary
    db.x("UPDATE runs SET finished_at=CURRENT_TIMESTAMP, score=?, xp=?, state=? WHERE id=?",
         (score, summary["xp_total_gained"], jd(state), run_id))
    return summary
