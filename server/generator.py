"""Генерация курса: разбор материалов -> структура -> планы модулей -> уроки (лениво, с предзагрузкой) + адаптация."""
import asyncio
import random
import re
import traceback

from . import config, grading, ingest, prompts, svgtools
from .db import db, jd, jl
from .llm import LLMError, LLMUnavailable, llm

_tasks: set[asyncio.Task] = set()
_locks: dict[str, asyncio.Lock] = {}

TYPE_TITLES = {
    "intro_test": "Входной тест",
    "terms": "Термины",
    "terms_test": "Проверка терминов",
    "practice": "Практика",
    "methods_test": "Тест на методы",
    "final_test": "Контрольный тест",
}
TYPE_MINUTES = {"intro_test": 3, "terms": 5, "terms_test": 2, "practice": 5, "methods_test": 2, "final_test": 6}


def lock(key: str) -> asyncio.Lock:
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]


_loop: asyncio.AbstractEventLoop | None = None


def set_loop(loop: asyncio.AbstractEventLoop):
    global _loop
    _loop = loop


def spawn(coro):
    """Запустить фоновую корутину; можно звать и из потока (тогда — в главный цикл приложения)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run_coroutine_threadsafe(coro, _loop)
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return t


def course_status(course_id: int, text: str, status: str | None = None, error: str | None = None):
    if status:
        db.x("UPDATE courses SET status=?, status_text=?, error=? WHERE id=?", (status, text, error, course_id))
    else:
        db.x("UPDATE courses SET status_text=? WHERE id=?", (text, course_id))


# ======================================================================= профиль ученика
def learner_profile(user_id: int, course_id: int) -> str:
    rows = db.q("""SELECT concept, COUNT(*) n, AVG(correct) acc FROM answers
                   WHERE user_id=? AND course_id=? AND concept IS NOT NULL AND concept<>'' GROUP BY concept""",
                (user_id, course_id))
    weak = sorted([r for r in rows if r["acc"] < 0.6], key=lambda r: r["acc"])[:8]
    strong = [r for r in rows if r["acc"] >= 0.85 and r["n"] >= 2][:8]
    recent = db.q("SELECT correct FROM answers WHERE user_id=? AND course_id=? ORDER BY id DESC LIMIT 30", (user_id, course_id))
    notes = (db.val("SELECT notes FROM courses WHERE id=?", (course_id,)) or "").strip()
    lines = []
    if recent:
        acc = sum(r["correct"] for r in recent) / len(recent)
        lines.append(f"- недавняя точность ответов: {round(acc * 100)}% (последние {len(recent)} ответов)")
    if weak:
        lines.append("- слабые понятия (нужны повторы и новые ракурсы): " + ", ".join(f"{r['concept']} ({round(r['acc'] * 100)}%)" for r in weak))
    if strong:
        lines.append("- хорошо усвоено (не пережёвывать): " + ", ".join(r["concept"] for r in strong))
    if notes:
        lines.append("- заметки методиста: " + notes)
    return "\n".join(lines)


def weak_concepts(user_id: int, course_id: int, module_terms: list[str] | None = None, limit: int = 4) -> list[str]:
    rows = db.q("""SELECT concept, AVG(correct) acc, COUNT(*) n FROM answers
                   WHERE user_id=? AND course_id=? AND concept<>'' GROUP BY concept HAVING acc<0.7 ORDER BY acc""",
                (user_id, course_id))
    names = [r["concept"] for r in rows]
    if module_terms is not None:
        low = {t.lower(): t for t in module_terms}
        names = [low[n.lower()] for n in names if n.lower() in low]
    return names[:limit]


# ======================================================================= контекст модуля
def module_context(module: dict, course_id: int, limit: int = config.MODULE_CONTEXT_CHARS) -> str:
    ids = jl(module.get("chunk_ids"), []) or []
    chunks = db.q("SELECT idx, text FROM chunks WHERE course_id=? ORDER BY idx", (course_id,))
    by_idx = {c["idx"]: c["text"] for c in chunks}
    picked = [(i, by_idx[i]) for i in ids if i in by_idx]
    if not picked:
        picked = [(c["idx"], c["text"]) for c in chunks[:3]]
    total = sum(len(t) for _, t in picked)
    out = []
    if total <= limit:
        for i, t in picked:
            out.append(f"[#{i}]\n{t}")
    else:
        per = max(600, limit // len(picked))
        for i, t in picked:
            out.append(f"[#{i}]\n{t[:per]}" + (" …" if len(t) > per else ""))
    return "\n\n".join(out)


# ======================================================================= нормализация ответов модели
def _clean(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _uniq(items: list[str], banned: set[str] | None = None) -> list[str]:
    seen = set(banned or set())
    out = []
    for it in items:
        k = grading.norm(it)
        if k and k not in seen:
            seen.add(k)
            out.append(it)
    return out


def norm_question(q: dict, rng: random.Random) -> dict:
    kind = _clean(q.get("kind")).lower() or "single"
    prompt = _clean(q.get("prompt") or q.get("question"))
    if not prompt:
        raise ValueError("пустой вопрос")
    out = {"kind": kind, "concept": _clean(q.get("concept")), "prompt": prompt,
           "explanation": _clean(q.get("explanation"))}
    if kind == "fill":
        accept = q.get("accept") or q.get("answers") or q.get("answer") or q.get("correct")
        if isinstance(accept, str):
            accept = [accept]
        accept = _uniq([_clean(a) for a in (accept or []) if _clean(a)])
        if not accept:
            raise ValueError("fill без ответа")
        out["accept"] = accept[:6]
        return out
    if kind not in ("single", "multi", "image"):
        kind = "single"
        out["kind"] = "single"
    correct = q.get("correct")
    wrong = q.get("wrong") or q.get("distractors") or []
    options = q.get("options")
    if correct is None and options is not None and q.get("answer") is not None:
        # модель вернула options+answer — приводим к correct/wrong
        ans = q.get("answer")
        ans_list = ans if isinstance(ans, list) else [ans]
        idxs = []
        for a in ans_list:
            if isinstance(a, int) or (isinstance(a, str) and a.strip().isdigit()):
                n = int(a)
                idxs.append(n if 0 <= n < len(options) else n - 1)
            else:
                for i, o in enumerate(options):
                    if grading.norm(str(o)) == grading.norm(str(a)):
                        idxs.append(i)
        correct = [options[i] for i in idxs if 0 <= i < len(options)]
        wrong = [o for i, o in enumerate(options) if i not in idxs]
    if isinstance(correct, str):
        correct = [correct]
    correct = _uniq([_clean(c) for c in (correct or []) if _clean(c)])
    wrong = _uniq([_clean(w) for w in wrong if _clean(w)], {grading.norm(c) for c in correct})
    if not correct or len(wrong) < 2:
        raise ValueError("мало вариантов ответа")
    if kind in ("single", "image"):
        correct = correct[:1]
        wrong = wrong[:3]
    else:
        if len(correct) < 2:  # multi с одним верным — превращаем в single
            out["kind"] = "single"
            wrong = wrong[:3]
        else:
            correct, wrong = correct[:3], wrong[:3]
    opts = [(c, True) for c in correct] + [(w, False) for w in wrong]
    rng.shuffle(opts)
    out["options"] = [o for o, _ in opts]
    idxs = [i for i, (_, ok) in enumerate(opts) if ok]
    out["answer"] = idxs if out["kind"] == "multi" else idxs[0]
    if out["kind"] == "image":
        svg = svgtools.sanitize(q.get("svg"))
        if svg:
            out["svg"] = svg
        else:
            out["kind"] = "single"  # картинка не получилась — обычный вопрос
            out["prompt"] = prompt
    return out


def norm_questions(obj: dict, minimum: int, seed: int | None = None, maximum: int = 14) -> list[dict]:
    rng = random.Random(seed)
    raw = obj.get("questions") if isinstance(obj, dict) else None
    if not isinstance(raw, list):
        raise ValueError("нет массива questions")
    out = []
    for q in raw:
        if not isinstance(q, dict):
            continue
        try:
            out.append(norm_question(q, rng))
        except ValueError:
            continue
    if len(out) < minimum:
        raise ValueError(f"получено {len(out)} корректных вопросов, нужно минимум {minimum}")
    return out[:maximum]


def _match_term(name: str, items: list[dict]) -> dict | None:
    n = grading.norm(name)
    for it in items:
        if grading.norm(it.get("term", "")) == n:
            return it
    for it in items:
        t = grading.norm(it.get("term", ""))
        if n and (n in t or t in n):
            return it
    return None


def norm_terms(obj: dict, requested: list[dict]) -> list[dict]:
    raw = obj.get("terms") if isinstance(obj, dict) else None
    if not isinstance(raw, list) or not raw:
        raise ValueError("нет массива terms")
    out = []
    for i, req in enumerate(requested):
        it = _match_term(req["term"], raw) or (raw[i] if i < len(raw) and isinstance(raw[i], dict) else None)
        if not it:
            raise ValueError(f"нет термина «{req['term']}»")
        definition = _clean(it.get("definition"))
        if len(definition) < 15:
            raise ValueError(f"нет определения для «{req['term']}»")
        kps = [_clean(k) for k in (it.get("key_points") or []) if _clean(k)][:6]
        out.append({
            "term": req["term"],
            "definition": definition,
            "key_points": kps,
            "example": _clean(it.get("example")),
            "wrong_terms": _uniq([_clean(x) for x in (it.get("wrong_terms") or []) if _clean(x)], {grading.norm(req["term"])})[:4],
            "wrong_definitions": _uniq([_clean(x) for x in (it.get("wrong_definitions") or []) if _clean(x)], {grading.norm(definition)})[:4],
        })
    return out


def norm_plan(obj: dict) -> dict:
    raw = obj.get("terms") if isinstance(obj, dict) else None
    if not isinstance(raw, list):
        raise ValueError("нет массива terms")
    terms, seen = [], set()
    for t in raw:
        if isinstance(t, str):
            t = {"term": t}
        if not isinstance(t, dict):
            continue
        name = _clean(t.get("term") or t.get("name"))
        if not name or grading.norm(name) in seen:
            continue
        seen.add(grading.norm(name))
        terms.append({"term": name, "gist": _clean(t.get("gist") or t.get("description"))})
    if len(terms) < 2:
        raise ValueError("слишком мало терминов")
    try:
        pt = int(obj.get("practice_tasks") or 0)
    except (TypeError, ValueError):
        pt = 0
    return {"terms": terms[:9], "practice_tasks": max(0, min(2, pt)), "focus": _clean(obj.get("focus"))}


def norm_outline(obj: dict, n_chunks: int) -> dict:
    mods = obj.get("modules") if isinstance(obj, dict) else None
    if not isinstance(mods, list) or not mods:
        raise ValueError("нет модулей")
    out = []
    for m in mods[:12]:
        if not isinstance(m, dict) or not _clean(m.get("title")):
            continue
        ids = []
        for x in m.get("chunk_ids") or []:
            try:
                n = int(re.sub(r"\D", "", str(x)) or 0)
            except ValueError:
                continue
            if 1 <= n <= n_chunks and n not in ids:
                ids.append(n)
        out.append({
            "title": _clean(m["title"])[:120],
            "summary": _clean(m.get("summary")),
            "objectives": [_clean(o) for o in (m.get("objectives") or []) if _clean(o)][:5],
            "chunk_ids": ids,
            "practical": bool(m.get("practical")),
        })
    if not out:
        raise ValueError("модули пусты")
    # модули без источников — поровну
    if any(not m["chunk_ids"] for m in out):
        per = max(1, n_chunks // len(out))
        for i, m in enumerate(out):
            if not m["chunk_ids"]:
                m["chunk_ids"] = list(range(i * per + 1, min(n_chunks, (i + 1) * per) + 1)) or [1]
    return {
        "title": _clean(obj.get("title")) or "Новый курс",
        "description": _clean(obj.get("description")),
        "icon": (_clean(obj.get("icon")) or "📘")[:4],
        "has_practice": bool(obj.get("has_practice")),
        "practice_kind": _clean(obj.get("practice_kind")) if _clean(obj.get("practice_kind")) in ("code", "problems") else "none",
        "modules": out,
    }


# ======================================================================= сборка курса
async def build_course(course_id: int):
    c = db.one("SELECT * FROM courses WHERE id=?", (course_id,))
    if not c:
        return
    try:
        if not llm.configured():
            raise LLMUnavailable("Не задан ключ OpenRouter. Откройте «Настройки» и вставьте ключ — затем нажмите «Повторить».")
        course_status(course_id, "Читаю материалы…")
        sources = db.q("SELECT id, filename, text FROM sources WHERE course_id=?", (course_id,))
        if not db.val("SELECT COUNT(*) FROM chunks WHERE course_id=?", (course_id,), 0):
            idx = 0
            for s in sources:
                for piece in ingest.chunk_text(s["text"]):
                    idx += 1
                    db.x("INSERT INTO chunks(course_id,source_id,idx,text) VALUES(?,?,?,?)", (course_id, s["id"], idx, piece))
        chunks = db.q("SELECT idx, text, digest FROM chunks WHERE course_id=? ORDER BY idx", (course_id,))
        total = sum(len(x["text"]) for x in chunks)
        lang = ingest.detect_language("\n".join(x["text"] for x in chunks[:5]))
        db.x("UPDATE courses SET language=? WHERE id=?", (lang, course_id))
        sysmsg = prompts.system(lang)

        full = total <= config.FULL_TEXT_LIMIT
        if full:
            material = "\n\n".join(f"[#{x['idx']}]\n{x['text']}" for x in chunks)
        else:
            # map: сжатые конспекты порциями по 8 фрагментов; запросы идут по два параллельно (семафор в llm)
            pending = [x for x in chunks if not x["digest"]]
            batches = [pending[b:b + 8] for b in range(0, len(pending), 8)]
            done_n = 0

            async def digest_batch(batch):
                nonlocal done_n
                obj = await llm.chat_json(sysmsg, prompts.digest([(x["idx"], x["text"][:3500]) for x in batch]),
                                          task="digest", ctx={"chunks": [(x["idx"], x["text"]) for x in batch]},
                                          max_tokens=4000)
                got = {int(re.sub(r"\D", "", str(d.get("id")) or "0") or 0): d for d in (obj.get("digests") or []) if isinstance(d, dict)}
                for x in batch:
                    d = got.get(x["idx"], {})
                    dg = _clean(d.get("summary")) + (" Термины: " + ", ".join(map(str, d.get("terms") or [])) if d.get("terms") else "")
                    if d.get("practice"):
                        dg += " [есть практика]"
                    db.x("UPDATE chunks SET digest=? WHERE course_id=? AND idx=?", (dg or x["text"][:300], course_id, x["idx"]))
                done_n += 1
                course_status(course_id, f"Конспектирую материалы ({done_n}/{len(batches)})…")

            course_status(course_id, f"Конспектирую материалы (0/{len(batches)})…")
            await asyncio.gather(*(digest_batch(b) for b in batches))
            chunks = db.q("SELECT idx, text, digest FROM chunks WHERE course_id=? ORDER BY idx", (course_id,))
            material = "\n".join(f"[#{x['idx']}] {x['digest']}" for x in chunks)

        course_status(course_id, "Составляю план курса…")
        names = [s["filename"] for s in sources]
        obj = await llm.chat_json(sysmsg, prompts.outline(material, names, c.get("title") if c.get("title") != "Новый курс" else "", full),
                                  task="outline", ctx={"chunks": [(x["idx"], x["text"]) for x in chunks], "filenames": names},
                                  validate=lambda o: norm_outline(o, len(chunks)), max_tokens=4000)
        with db.tx():
            db.x("DELETE FROM modules WHERE course_id=?", (course_id,))
            for i, m in enumerate(obj["modules"]):
                db.x("""INSERT INTO modules(course_id,idx,title,summary,objectives,chunk_ids,practical) VALUES(?,?,?,?,?,?,?)""",
                     (course_id, i, m["title"], m["summary"], jd(m["objectives"]), jd(m["chunk_ids"]), int(m["practical"])))
            title = c["title"] if c.get("title") and c["title"] != "Новый курс" else obj["title"]
            db.x("""UPDATE courses SET title=?, description=?, icon=?, has_practice=?, practice_kind=?, status='ready',
                    status_text='', error=NULL WHERE id=?""",
                 (title, obj["description"], obj["icon"], int(obj["has_practice"]), obj["practice_kind"], course_id))
        # заранее готовим первый модуль
        spawn(prepare_next(course_id))
    except LLMUnavailable as e:
        course_status(course_id, "", "error", str(e))
    except Exception as e:
        traceback.print_exc()
        course_status(course_id, "", "error", f"Не удалось составить курс: {e}")


# ======================================================================= порядок уроков
def ordered_lessons(course_id: int) -> list[dict]:
    return db.q("""SELECT l.*, m.idx AS midx FROM lessons l JOIN modules m ON m.id=l.module_id
                   WHERE l.course_id=? ORDER BY m.idx, l.idx""", (course_id,))


def next_lesson(course_id: int) -> dict | None:
    for l in ordered_lessons(course_id):
        if l["status"] != "done":
            return l
    return None


def next_unplanned_module(course_id: int) -> dict | None:
    for m in db.q("SELECT * FROM modules WHERE course_id=? ORDER BY idx", (course_id,)):
        if m["status"] == "new":
            return m
        if m["status"] != "done":
            return None  # текущий модуль ещё идёт
    return None


# ======================================================================= планирование модуля
def _split_groups(terms: list[dict], size: int = 3) -> list[list[dict]]:
    n = len(terms)
    groups = max(1, -(-n // size))
    base, extra = divmod(n, groups)
    out, i = [], 0
    for g in range(groups):
        k = base + (1 if g < extra else 0)
        out.append(terms[i:i + k])
        i += k
    return out


def _add_lesson(course_id, module_id, idx, typ, title, meta=None, minutes=None):
    return db.x("INSERT INTO lessons(course_id,module_id,idx,type,title,minutes,meta) VALUES(?,?,?,?,?,?,?)",
                (course_id, module_id, idx, typ, title, minutes or TYPE_MINUTES[typ], jd(meta or {})))


def build_skeleton(course_id: int, module_id: int, plan: dict, *, with_intro=True):
    idx = 0
    if with_intro:
        _add_lesson(course_id, module_id, idx, "intro_test", "Входной тест: что уже знаешь", {"terms": [t["term"] for t in plan["terms"]]})
        idx += 1
    for g, group in enumerate(_split_groups(plan["terms"]), 1):
        names = ", ".join(t["term"] for t in group)
        tid = _add_lesson(course_id, module_id, idx, "terms", f"Термины: {names}"[:110], {"terms": group, "n": g})
        idx += 1
        _add_lesson(course_id, module_id, idx, "terms_test", "Проверка: термины и описания", {"for": tid})
        idx += 1
    for p in range(plan.get("practice_tasks") or 0):
        pid = _add_lesson(course_id, module_id, idx, "practice", "Практика" + (f" {p + 1}" if plan["practice_tasks"] > 1 else ""), {"n": p + 1})
        idx += 1
        _add_lesson(course_id, module_id, idx, "methods_test", "Тест: методы решения", {"for": pid})
        idx += 1
    _add_lesson(course_id, module_id, idx, "final_test", "Контрольный тест модуля", {})


async def plan_module(module_id: int):
    async with lock(f"module:{module_id}"):
        m = db.one("SELECT * FROM modules WHERE id=?", (module_id,))
        if not m or m["status"] != "new":
            return
        c = db.one("SELECT * FROM courses WHERE id=?", (m["course_id"],))
        if m["kind"] == "review":
            plan = jl(m["plan"], {})
            with db.tx():
                build_skeleton(c["id"], module_id, plan, with_intro=False)
                db.x("UPDATE modules SET status='planned' WHERE id=?", (module_id,))
            return
        ctx_text = module_context(m, c["id"])
        profile = learner_profile(c["user_id"], c["id"])
        done_terms = []
        for r in db.q("SELECT plan FROM modules WHERE course_id=? AND status IN ('done','planned') AND id<>? ORDER BY idx", (c["id"], module_id)):
            done_terms += [t["term"] for t in (jl(r["plan"], {}) or {}).get("terms", [])]
        m2 = dict(m)
        m2["objectives"] = jl(m["objectives"], [])
        m2["_course_practice"] = bool(c["has_practice"] and m["practical"])
        def v(o):
            plan = norm_plan(o)
            plan["questions"] = norm_questions(o, 5, seed=module_id, maximum=10)
            return plan

        try:
            obj = await llm.chat_json(prompts.system(c["language"]),
                                      prompts.module_start(c["title"], m2, ctx_text, profile, done_terms),
                                      task="module_start", ctx={"module": m2, "context": ctx_text, "done_terms": done_terms},
                                      validate=v, max_tokens=7000)
        except Exception as e:  # запоминаем причину, чтобы интерфейс не ждал вечно
            db.x("UPDATE modules SET plan=? WHERE id=?", (jd({"error": f"Не удалось подготовить модуль: {e}"[:400]}), module_id))
            raise
        questions = obj.pop("questions")
        if not (c["has_practice"] and m["practical"]):
            obj["practice_tasks"] = 0
        elif obj["practice_tasks"] == 0:
            obj["practice_tasks"] = 1
        with db.tx():
            db.x("UPDATE modules SET plan=?, status='planned' WHERE id=?", (jd(obj), module_id))
            build_skeleton(c["id"], module_id, obj)
            db.x("UPDATE lessons SET content=?, status='ready' WHERE module_id=? AND type='intro_test'",
                 (jd({"questions": questions}), module_id))


# ======================================================================= генерация уроков
def _module_terms_pool(module_id: int) -> list[dict]:
    """Все термины модуля с уже сгенерированными определениями."""
    pool = []
    for l in db.q("SELECT content FROM lessons WHERE module_id=? AND type='terms' AND content IS NOT NULL ORDER BY idx", (module_id,)):
        pool += (jl(l["content"], {}) or {}).get("terms", [])
    return pool


def build_terms_test(terms: list[dict], pool: list[dict], seed: int) -> list[dict]:
    rng = random.Random(seed)
    qs = []
    for t in terms:
        # 1) описание -> термин
        wrong = list(t.get("wrong_terms") or [])
        others = [p["term"] for p in pool if p["term"] != t["term"]]
        rng.shuffle(others)
        wrong = _uniq(wrong + others, {grading.norm(t["term"])})[:3]
        if len(wrong) >= 2:
            opts = [(t["term"], True)] + [(w, False) for w in wrong]
            rng.shuffle(opts)
            qs.append({"kind": "single", "concept": t["term"], "prompt": f"Какой термин соответствует описанию?\n«{t['definition']}»",
                       "options": [o for o, _ in opts], "answer": [i for i, (_, ok) in enumerate(opts) if ok][0],
                       "explanation": f"Это «{t['term']}». " + (t.get("example") or "")})
        # 2) термин -> описание
        wd = list(t.get("wrong_definitions") or [])
        if len(wd) < 2:
            wd += [p["definition"] for p in pool if p["term"] != t["term"]][:3]
        wd = _uniq(wd, {grading.norm(t["definition"])})[:3]
        if len(wd) >= 2:
            opts = [(t["definition"], True)] + [(w, False) for w in wd]
            rng.shuffle(opts)
            qs.append({"kind": "single", "concept": t["term"], "prompt": f"Какое описание верно для термина «{t['term']}»?",
                       "options": [o for o, _ in opts], "answer": [i for i, (_, ok) in enumerate(opts) if ok][0],
                       "explanation": f"«{t['term']}» — {t['definition']}"})
    rng.shuffle(qs)
    return qs


def _pair_lesson(lesson_id: int, typ: str) -> dict | None:
    for l in db.q("SELECT * FROM lessons WHERE type=? AND module_id=(SELECT module_id FROM lessons WHERE id=?)", (typ, lesson_id)):
        if jl(l["meta"], {}).get("for") == lesson_id:
            return l
    return None


async def generate_lesson(lesson_id: int):
    """Сгенерировать содержимое урока (и парного теста). Идемпотентно."""
    async with lock(f"lesson:{lesson_id}"):
        l = db.one("SELECT * FROM lessons WHERE id=?", (lesson_id,))
        if not l or l["status"] in ("ready", "done"):
            return
        db.x("UPDATE lessons SET status='generating', error=NULL WHERE id=?", (lesson_id,))
        try:
            await _generate(l)
        except LLMUnavailable as e:
            db.x("UPDATE lessons SET status='failed', error=? WHERE id=?", (str(e), lesson_id))
        except Exception as e:
            traceback.print_exc()
            db.x("UPDATE lessons SET status='failed', error=? WHERE id=?", (f"Не удалось подготовить урок: {e}", lesson_id))


async def _generate(l: dict):
    m = db.one("SELECT * FROM modules WHERE id=?", (l["module_id"],))
    c = db.one("SELECT * FROM courses WHERE id=?", (l["course_id"],))
    meta = jl(l["meta"], {})
    mod = dict(m)
    mod["objectives"] = jl(m["objectives"], [])
    plan = jl(m["plan"], {}) or {}
    plan_terms = plan.get("terms", [])
    ctx_text = module_context(m, c["id"])
    profile = learner_profile(c["user_id"], c["id"])
    sysmsg = prompts.system(c["language"])
    typ = l["type"]

    if typ == "intro_test":
        obj = await llm.chat_json(sysmsg, prompts.intro_test(c["title"], mod, plan_terms, ctx_text, profile),
                                  task="intro_test", ctx={"module": mod, "terms": plan_terms, "context": ctx_text},
                                  validate=lambda o: {"questions": norm_questions(o, 5, seed=l["id"], maximum=10)}, max_tokens=5000)
        content = obj

    elif typ == "terms":
        # все ещё не готовые уроки «термины» модуля делаем ОДНИМ запросом (экономим лимит бесплатного API)
        async with lock(f"terms:{m['id']}"):
            cur = db.one("SELECT content FROM lessons WHERE id=?", (l["id"],))
            if cur and cur["content"]:
                db.x("UPDATE lessons SET status='ready' WHERE id=? AND status='generating'", (l["id"],))
                return
            batch = [l]
            if not meta.get("remedial"):
                for x in db.q("SELECT * FROM lessons WHERE module_id=? AND type='terms' AND content IS NULL AND id<>? ORDER BY idx", (m["id"], l["id"])):
                    xm = jl(x["meta"], {})
                    if not xm.get("remedial") and x["status"] in ("pending", "failed"):
                        batch.append(x)
            batch = batch[:3]
            groups = [(b, (jl(b["meta"], {}).get("terms") or plan_terms[:3])) for b in batch]
            all_terms = [t for _, g in groups for t in g]
            known = [p["term"] for p in _module_terms_pool(m["id"])] + [t["term"] for t in plan_terms]
            obj = await llm.chat_json(sysmsg, prompts.terms_lesson(c["title"], mod, all_terms, ctx_text, profile, _uniq(known)),
                                      task="terms", ctx={"module": mod, "terms": all_terms, "context": ctx_text},
                                      validate=lambda o: {"terms": norm_terms(o, all_terms)}, max_tokens=7000)
            by_name = {grading.norm(t["term"]): t for t in obj["terms"]}
            pool = _module_terms_pool(m["id"])
            for b, g in groups:
                items = [by_name[grading.norm(t["term"])] for t in g]
                db.x("UPDATE lessons SET content=?, status='ready', error=NULL WHERE id=?", (jd({"terms": items}), b["id"]))
                pair = _pair_lesson(b["id"], "terms_test")
                if pair and not pair["content"]:  # парный тест строим локально — без лишнего вызова LLM
                    qs = build_terms_test(items, pool + obj["terms"], seed=b["id"])
                    db.x("UPDATE lessons SET content=?, status='ready' WHERE id=?", (jd({"questions": qs}), pair["id"]))
            return

    elif typ == "practice":
        prev = [jl(x["content"], {}).get("task", {}).get("title", "") for x in
                db.q("SELECT content FROM lessons WHERE course_id=? AND type='practice' AND content IS NOT NULL", (c["id"],))]
        terms = [t["term"] for t in plan_terms]

        def v(o):
            t = o.get("task") if isinstance(o, dict) else None
            if not isinstance(t, dict) or len(_clean(t.get("statement"))) < 20:
                raise ValueError("нет условия задачи")
            t["statement"] = str(t["statement"]).strip()
            t["title"] = _clean(t.get("title")) or "Практическое задание"
            t["starter"] = str(t.get("starter") or "")
            t["solution"] = str(t.get("solution") or "")
            t["hints"] = [_clean(h) for h in (t.get("hints") or []) if _clean(h)][:3]
            t["rubric"] = [_clean(h) for h in (t.get("rubric") or []) if _clean(h)][:6]
            t["methods"] = [{"name": _clean(x.get("name")), "when": _clean(x.get("when"))} for x in (t.get("methods") or []) if isinstance(x, dict) and _clean(x.get("name"))]
            t["kind"] = t.get("kind") if t.get("kind") in ("code", "problems") else "problems"
            t["language"] = _clean(t.get("language"))
            if not t["solution"]:
                raise ValueError("нет эталонного решения")
            return {"task": t, "questions": norm_questions(o, 3, seed=l["id"], maximum=7)}

        obj = await llm.chat_json(sysmsg, prompts.practice(c["title"], mod, c["practice_kind"], ctx_text, profile, terms, [p for p in prev if p]),
                                  task="practice", ctx={"module": mod, "terms": plan_terms, "context": ctx_text, "kind": c["practice_kind"]},
                                  validate=v, max_tokens=5000)
        content = {"task": obj["task"]}
        pair = _pair_lesson(l["id"], "methods_test")
        if pair and not pair["content"]:
            db.x("UPDATE lessons SET content=?, status='ready' WHERE id=?", (jd({"questions": obj["questions"]}), pair["id"]))

    elif typ == "final_test":
        terms = [t["term"] for t in plan_terms]
        asked = []
        for r in db.q("SELECT content FROM lessons WHERE module_id=? AND type IN ('intro_test','final_test') AND content IS NOT NULL", (m["id"],)):
            asked += [q.get("prompt", "") for q in (jl(r["content"], {}) or {}).get("questions", [])]
        weak = weak_concepts(c["user_id"], c["id"], terms)
        obj = await llm.chat_json(sysmsg, prompts.final_test(c["title"], mod, terms, ctx_text, profile, asked, weak),
                                  task="final_test", ctx={"module": mod, "terms": plan_terms, "context": ctx_text},
                                  validate=lambda o: {"questions": norm_questions(o, 5, seed=l["id"], maximum=10)}, max_tokens=5000)
        pool = _module_terms_pool(m["id"])
        # задания на термины (второй тип урока): слабые первыми
        rng = random.Random(l["id"])
        keyed = [((t["term"] not in weak), rng.random(), i, t) for i, t in enumerate(pool)]
        ranked = [k[3] for k in sorted(keyed, key=lambda k: k[:3])]
        wr = ranked[:3]
        content = {"questions": obj["questions"], "write_terms": wr,
                   "choose": build_terms_test(ranked[3:5] or ranked[:2], pool, seed=l["id"] + 7)[:3]}
    else:
        # парные тесты (terms_test / methods_test) готовятся вместе с «родителем»
        parent_id = meta.get("for")
        if parent_id:
            await _generate_parent_for_pair(parent_id)
            fresh = db.one("SELECT status, content FROM lessons WHERE id=?", (l["id"],))
            if fresh and fresh["content"]:
                return
        raise RuntimeError("парный тест не был создан")

    db.x("UPDATE lessons SET content=?, status='ready', error=NULL WHERE id=?", (jd(content), l["id"]))


async def _generate_parent_for_pair(parent_id: int):
    p = db.one("SELECT * FROM lessons WHERE id=?", (parent_id,))
    if p and p["status"] not in ("ready", "done"):
        await generate_lesson(parent_id)


# ======================================================================= предзагрузка
async def prefetch(course_id: int, ahead: int = 2):
    """Заранее готовим ближайшие уроки текущего модуля, чтобы ученик не ждал."""
    try:
        cnt = 0
        for l in ordered_lessons(course_id):
            if l["status"] == "done":
                continue
            if l["status"] in ("pending", "failed") and not l["content"]:
                # парные тесты создаются родителем
                meta = jl(l["meta"], {})
                if l["type"] in ("terms_test", "methods_test") and meta.get("for"):
                    parent = db.one("SELECT status FROM lessons WHERE id=?", (meta["for"],))
                    if parent and parent["status"] == "done":
                        await generate_lesson(l["id"])
                else:
                    await generate_lesson(l["id"])
            cnt += 1
            if cnt >= ahead:
                break
    except Exception:
        traceback.print_exc()


async def prepare_next(course_id: int):
    """Спланировать следующий модуль (если текущий закончен) и подготовить ближайшие уроки."""
    try:
        nm = next_unplanned_module(course_id)
        if nm:
            await plan_module(nm["id"])
        await prefetch(course_id)
    except Exception:
        traceback.print_exc()


# ======================================================================= после урока: адаптация
def module_summary_text(user_id: int, course_id: int, module_id: int) -> str:
    m = db.one("SELECT * FROM modules WHERE id=?", (module_id,))
    ls = db.q("SELECT type, title, score FROM lessons WHERE module_id=? AND status='done' ORDER BY idx", (module_id,))
    lines = [f"Модуль «{m['title']}»:"]
    for l in ls:
        if l["score"] is not None:
            lines.append(f"- {l['title']} ({l['type']}): {round(l['score'] * 100)}%")
    rows = db.q("""SELECT a.concept, AVG(a.correct) acc, COUNT(*) n FROM answers a JOIN lessons l ON l.id=a.lesson_id
                   WHERE a.user_id=? AND l.module_id=? AND a.concept<>'' GROUP BY a.concept ORDER BY acc""", (user_id, module_id))
    if rows:
        lines.append("По понятиям: " + "; ".join(f"{r['concept']} {round(r['acc'] * 100)}% ({r['n']})" for r in rows[:14]))
    return "\n".join(lines)


# ---- «закрепление»: повторять, пока результат не станет хорошим или заметно лучше первого ----
MASTERY_FIRST = 0.7      # первая попытка считается удачной от этого порога
MASTERY_TARGET = 0.8     # цель: дальше не повторяем
MASTERY_GAIN = 0.2       # «заметное улучшение» относительно первой попытки (при результате не ниже MASTERY_FIRST)
MASTERY_MAX_TRIES = 3    # максимум повторов на один урок
MODULE_MAX_RETRIES = 7   # максимум вставленных повторов на модуль целиком


def _root_id(l: dict) -> int:
    return (jl(l["meta"], {}) or {}).get("root") or l["id"]


def _score_history(root: int) -> list[float]:
    rows = db.q("""SELECT score FROM lessons WHERE status='done' AND score IS NOT NULL
                   AND (id=? OR json_extract(meta,'$.root')=?) ORDER BY id""", (root, root))
    return [r["score"] for r in rows]


def needs_more(hist: list[float]) -> bool:
    """Нужен ли ещё один повтор. hist — результаты попыток по порядку (первая — исходный урок)."""
    last, tries = hist[-1], len(hist) - 1
    if tries >= MASTERY_MAX_TRIES:
        return False
    if tries == 0:
        return last < MASTERY_FIRST
    if last >= MASTERY_TARGET:
        return False
    if last >= MASTERY_FIRST and last - hist[0] >= MASTERY_GAIN:
        return False  # результат заметно вырос — достаточно
    return True


def _lesson_weak_terms(user_id: int, lesson: dict, module_id: int, limit: int = 3) -> list[dict]:
    """Термины, на которых ученик ошибся в этом уроке (с определениями из модуля), худшие первыми."""
    rows = db.q("""SELECT concept, AVG(correct) acc FROM answers WHERE user_id=? AND lesson_id=? AND concept<>''
                   GROUP BY concept HAVING AVG(correct)<0.7 ORDER BY AVG(correct)""", (user_id, lesson["id"]))
    pool = {grading.norm(t["term"]): t for t in _module_terms_pool(module_id)}
    names = [r["concept"] for r in rows if grading.norm(r["concept"]) in pool]
    if not names:
        names = [w for w in weak_concepts(user_id, lesson["course_id"], [t["term"] for t in pool.values()], limit=limit) if grading.norm(w) in pool]
    if not names:
        names = [t["term"] for t in list(pool.values())[:limit]]
    return [{"term": pool[grading.norm(n)]["term"], "gist": pool[grading.norm(n)]["definition"][:120]} for n in names[:limit]]


def _insert_after(lesson: dict, specs: list[tuple]) -> list[int]:
    """Вставить уроки сразу после данного. specs: (type, title, meta|callable(prev_ids)->meta). Возвращает id новых уроков."""
    ids: list[int] = []
    with db.tx():
        db.x("UPDATE lessons SET idx=idx+? WHERE module_id=? AND idx>?", (len(specs), lesson["module_id"], lesson["idx"]))
        for i, (typ, title, meta) in enumerate(specs, 1):
            meta = meta(ids) if callable(meta) else meta
            ids.append(_add_lesson(lesson["course_id"], lesson["module_id"], lesson["idx"] + i, typ, title, meta))
        db.x("UPDATE modules SET retries=retries+1 WHERE id=?", (lesson["module_id"],))
    return ids


def _schedule_retry(c: dict, m: dict, l: dict) -> bool:
    """Если результат слабый — вставить после урока работу над ошибками и повторную проверку. True, если вставили."""
    if l["type"] not in ("terms_test", "methods_test", "final_test"):
        return False
    if (m["retries"] or 0) >= MODULE_MAX_RETRIES:
        return False
    root = _root_id(l)
    hist = _score_history(root)
    if not hist or not needs_more(hist):
        return False
    n = len(hist)  # номер следующей попытки
    if l["type"] == "methods_test":
        specs = [("practice", f"Практика: ещё одна задача (попытка {n + 1})", {"retry": True, "n": 100 + n}),
                 ("methods_test", f"Тест на методы (попытка {n + 1})", lambda ids: {"for": ids[0], "retry": True, "root": root})]
    else:
        group = _lesson_weak_terms(c["user_id"], l, m["id"])
        if not group:
            return False
        names = ", ".join(t["term"] for t in group)
        specs = [("terms", "Работа над ошибками: " + names, {"terms": group, "remedial": True, "retry": True}),
                 ("terms_test", f"Проверка после повторения (попытка {n + 1})" if l["type"] == "terms_test" else "Проверка после повторения",
                  lambda ids: {"for": ids[0], "retry": True, **({"root": root} if l["type"] == "terms_test" else {})})]
        if l["type"] == "final_test":
            specs.append(("final_test", f"Повторный контрольный тест (попытка {n + 1})", {"retry": True, "root": root}))
    _insert_after(l, specs)
    return True


async def after_lesson(lesson_id: int):
    """Хук после завершения урока: закрепление (повторы до улучшения), закрытие модуля, адаптация, предзагрузка."""
    try:
        l = db.one("SELECT * FROM lessons WHERE id=?", (lesson_id,))
        if not l:
            return
        c = db.one("SELECT * FROM courses WHERE id=?", (l["course_id"],))
        m = db.one("SELECT * FROM modules WHERE id=?", (l["module_id"],))
        if l["type"] == "final_test":
            await _finish_module(c, m, l)
        else:
            _schedule_retry(c, m, l)
            await prefetch(c["id"])
    except Exception:
        traceback.print_exc()


async def _finish_module(c: dict, m: dict, l: dict):
    if _schedule_retry(c, m, l):
        await prefetch(c["id"])
        return
    db.x("UPDATE modules SET status='done' WHERE id=?", (m["id"],))
    left = db.q("SELECT * FROM modules WHERE course_id=? AND status<>'done' ORDER BY idx", (c["id"],))
    if not left:
        db.x("UPDATE courses SET status='completed' WHERE id=?", (c["id"],))
        return
    # анализ успеваемости и корректировка продолжения (вызов модели — только если ученик буксует, чтобы беречь лимиты)
    acc = db.val("""SELECT AVG(a.correct) FROM answers a JOIN lessons l ON l.id=a.lesson_id
                    WHERE a.user_id=? AND l.module_id=?""", (c["user_id"], m["id"]), 1.0)
    done_modules = db.val("SELECT COUNT(*) FROM modules WHERE course_id=? AND status='done'", (c["id"],), 0)
    if acc >= 0.8 and done_modules % 3 != 0:
        msg = ("Отличный результат — иду дальше в том же темпе!" if acc >= 0.9 else "Хорошо усвоено. Двигаемся дальше!")
        db.x("UPDATE modules SET plan=json_set(COALESCE(plan,'{}'), '$.message', ?) WHERE id=?", (msg, m["id"]))
        await prepare_next(c["id"])
        return
    try:
        summary = module_summary_text(c["user_id"], c["id"], m["id"])
        remaining = [{"title": x["title"], "summary": x["summary"]} for x in left if x["status"] == "new"]
        obj = await llm.chat_json(prompts.system(c["language"]), prompts.adapt(c["title"], summary, remaining, c["notes"]),
                                  task="adapt", ctx={"summary": summary, "remaining": remaining}, max_tokens=1200)
        notes = _clean(obj.get("notes"))[:700]
        if notes:
            db.x("UPDATE courses SET notes=? WHERE id=?", (notes, c["id"]))
        db.x("UPDATE modules SET plan=json_set(COALESCE(plan,'{}'), '$.message', ?) WHERE id=?", (_clean(obj.get("message")), m["id"]))
        rv = obj.get("review")
        if isinstance(rv, dict) and _clean(rv.get("title")) and left[0]["status"] == "new":
            weak = weak_concepts(c["user_id"], c["id"], None, limit=4)
            pool = {grading.norm(t["term"]): t for t in
                    [t for mm in db.q("SELECT id FROM modules WHERE course_id=? AND status='done'", (c["id"],)) for t in _module_terms_pool(mm["id"])]}
            group = [{"term": pool[grading.norm(w)]["term"], "gist": pool[grading.norm(w)]["definition"][:120]} for w in weak if grading.norm(w) in pool]
            if len(group) >= 2:
                nxt = left[0]
                with db.tx():
                    db.x("UPDATE modules SET idx=idx+1 WHERE course_id=? AND idx>=?", (c["id"], nxt["idx"]))
                    ids = []
                    for mm in db.q("SELECT chunk_ids FROM modules WHERE course_id=? AND status='done'", (c["id"],)):
                        ids += jl(mm["chunk_ids"], [])
                    db.x("""INSERT INTO modules(course_id,idx,title,summary,objectives,chunk_ids,practical,kind,plan) VALUES(?,?,?,?,?,?,0,'review',?)""",
                         (c["id"], nxt["idx"], _clean(rv["title"])[:120], _clean(rv.get("summary")), "[]",
                          jd(sorted(set(ids))[:12]), jd({"terms": group[:6], "practice_tasks": 0})))
    except LLMUnavailable:
        pass
    except Exception:
        traceback.print_exc()
    await prepare_next(c["id"])
