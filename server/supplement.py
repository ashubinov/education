"""Дополнение готового курса новыми материалами и предгенерация курса целиком (для каталога)."""
import asyncio
import re
import traceback

from . import config, ingest, prompts
from .db import db, jd, jl
from .generator import (_clean, course_status, generate_lesson, norm_outline, plan_module, prepare_next, spawn)
from .llm import llm


async def _digest_chunks(course_id: int, chunks: list[dict], sysmsg: str):
    """Сжатые конспекты для фрагментов без digest (порциями по 8, по два запроса параллельно)."""
    pending = [x for x in chunks if not x.get("digest")]
    batches = [pending[b:b + 8] for b in range(0, len(pending), 8)]
    done_n = 0

    async def one(batch):
        nonlocal done_n
        obj = await llm.chat_json(sysmsg, prompts.digest([(x["idx"], x["text"][:3500]) for x in batch]), task="digest",
                                  ctx={"chunks": [(x["idx"], x["text"]) for x in batch]}, max_tokens=4000)
        got = {int(re.sub(r"\D", "", str(d.get("id")) or "0") or 0): d for d in (obj.get("digests") or []) if isinstance(d, dict)}
        for x in batch:
            d = got.get(x["idx"], {})
            dg = _clean(d.get("summary")) + (" Термины: " + ", ".join(map(str, d.get("terms") or [])) if d.get("terms") else "")
            if d.get("practice"):
                dg += " [есть практика]"
            x["digest"] = dg or x["text"][:300]
            db.x("UPDATE chunks SET digest=? WHERE course_id=? AND idx=?", (x["digest"], course_id, x["idx"]))
        done_n += 1
        course_status(course_id, f"Конспектирую новые материалы ({done_n}/{len(batches)})…")

    if batches:
        await asyncio.gather(*(one(b) for b in batches))


async def supplement_course(course_id: int, source_ids: list[int]):
    """Добавить в готовый курс модули по новым файлам (источники уже сохранены в таблице sources)."""
    c = db.one("SELECT * FROM courses WHERE id=?", (course_id,))
    if not c:
        return
    try:
        course_status(course_id, "Читаю новые материалы…", "processing")
        start = db.val("SELECT MAX(idx) FROM chunks WHERE course_id=?", (course_id,), 0) or 0
        marks = ",".join(str(int(i)) for i in source_ids) or "0"
        sources = db.q(f"SELECT id, filename, text FROM sources WHERE id IN ({marks})")
        idx = start
        for s in sources:
            for piece in ingest.chunk_text(s["text"]):
                idx += 1
                db.x("INSERT INTO chunks(course_id,source_id,idx,text) VALUES(?,?,?,?)", (course_id, s["id"], idx, piece))
        new_chunks = db.q("SELECT idx, text, digest FROM chunks WHERE course_id=? AND idx>? ORDER BY idx", (course_id, start))
        if not new_chunks:
            raise ValueError("в новых файлах нет текста")
        sysmsg = prompts.system(c["language"])
        full = sum(len(x["text"]) for x in new_chunks) <= config.FULL_TEXT_LIMIT
        if full:
            material = "\n\n".join(f"[#{x['idx']}]\n{x['text']}" for x in new_chunks)
        else:
            await _digest_chunks(course_id, new_chunks, sysmsg)
            material = "\n".join(f"[#{x['idx']}] {x['digest']}" for x in new_chunks)
        course_status(course_id, "Дополняю план курса…")
        existing = [m["title"] for m in db.q("SELECT title FROM modules WHERE course_id=? ORDER BY idx", (course_id,))]
        names = [s["filename"] for s in sources]

        def v(o):
            res = norm_outline({**o, "title": "x"}, idx)
            for m in res["modules"]:  # только фрагменты нового материала
                m["chunk_ids"] = [i for i in m["chunk_ids"] if i > start] or list(range(start + 1, idx + 1))
            return res

        obj = await llm.chat_json(sysmsg, prompts.supplement(material, names, existing, full), task="supplement",
                                  ctx={"chunks": [(x["idx"], x["text"]) for x in new_chunks], "filenames": names}, validate=v, max_tokens=4000)
        base = db.val("SELECT MAX(idx) FROM modules WHERE course_id=?", (course_id,), None)
        base = -1 if base is None else base
        with db.tx():
            for i, m in enumerate(obj["modules"]):
                db.x("INSERT INTO modules(course_id,idx,title,summary,objectives,chunk_ids,practical) VALUES(?,?,?,?,?,?,?)",
                     (course_id, base + 1 + i, m["title"], m["summary"], jd(m["objectives"]), jd(m["chunk_ids"]), int(m["practical"])))
            if obj["has_practice"] and not c["has_practice"]:
                db.x("UPDATE courses SET has_practice=1, practice_kind='problems' WHERE id=?", (course_id,))
            db.x("UPDATE courses SET status='ready', status_text='', error=NULL WHERE id=?", (course_id,))
        spawn(prepare_next(course_id))
    except Exception as e:
        traceback.print_exc()
        # курс остаётся рабочим; причину покажем один раз на странице курса
        db.x("UPDATE courses SET status='ready', status_text='', error=? WHERE id=?", (f"Не удалось дополнить курс: {e}"[:400], course_id))


async def pregenerate_course(course_id: int, log=print):
    """Спланировать все модули и сгенерировать все уроки заранее, чтобы копии курса не тратили запросы к модели."""
    mods = db.q("SELECT id, title FROM modules WHERE course_id=? ORDER BY idx", (course_id,))
    for m in mods:  # планируем по порядку: следующий модуль учитывает термины предыдущих
        await plan_module(m["id"])
        err = (jl(db.val("SELECT plan FROM modules WHERE id=?", (m["id"],)), {}) or {}).get("error")
        if err:
            raise RuntimeError(err)
        log(f"  план модуля «{m['title']}» готов")

    async def module_lessons(m):
        for l in db.q("SELECT id, type, title FROM lessons WHERE module_id=? ORDER BY idx", (m["id"],)):
            st = None
            for _ in range(3):
                await generate_lesson(l["id"])
                st = db.one("SELECT status, error FROM lessons WHERE id=?", (l["id"],))
                if st["status"] in ("ready", "done"):
                    break
                db.x("UPDATE lessons SET status='pending' WHERE id=?", (l["id"],))
                await asyncio.sleep(3)
            else:
                raise RuntimeError(f"урок «{l['title']}» не удалось сгенерировать: {st['error']}")
            log(f"  урок {l['type']}: {l['title'][:60]}")

    await asyncio.gather(*(module_lessons(m) for m in mods))
