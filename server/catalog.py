"""Каталог готовых курсов: у каждого свой номер, пользователь находит курс по номеру и добавляет себе копию.

Курс-шаблон принадлежит служебному пользователю «catalog». Копия содержит уже сгенерированные уроки (модель не вызывается заново),
а прогресс, адаптация и XP у каждого пользователя свои. Повторно добавить тот же курс нельзя — вернётся уже существующая копия;
вместо этого есть «Пройти заново» (сброс прогресса).
"""
import hashlib
import json
import re

from . import auth
from . import gamification as gm
from .db import db, jd, jl

FIRST_NUMBER = 101
COURSE_FIELDS = ("title", "description", "icon", "language", "has_practice", "practice_kind")


def system_user_id() -> int:
    u = db.one("SELECT id FROM users WHERE username=?", (auth.SYSTEM_USERNAME,))
    if u:
        return u["id"]
    return db.x("INSERT INTO users(username,display_name,pass_hash,salt,avatar,is_admin) VALUES(?,?,?,?,?,0)",
                (auth.SYSTEM_USERNAME, "Каталог", "!", "00", "📚"))


def next_number() -> int:
    return (db.val("SELECT MAX(catalog_no) FROM courses WHERE is_template=1", default=None) or (FIRST_NUMBER - 1)) + 1


# ----------------------------------------------------------------------- копирование дерева курса
def read_tree(course_id: int) -> dict:
    c = db.one("SELECT * FROM courses WHERE id=?", (course_id,))
    if not c:
        raise KeyError("course")
    return {
        "course": {k: c[k] for k in COURSE_FIELDS},
        "sources": db.q("SELECT id, filename, chars, text FROM sources WHERE course_id=? ORDER BY id", (course_id,)),
        "chunks": db.q("SELECT source_id, idx, text, digest FROM chunks WHERE course_id=? ORDER BY idx", (course_id,)),
        "modules": db.q("SELECT id, idx, title, summary, objectives, chunk_ids, practical, plan, kind FROM modules WHERE course_id=? ORDER BY idx", (course_id,)),
        "lessons": db.q("SELECT id, module_id, idx, type, title, minutes, content, meta FROM lessons WHERE course_id=? ORDER BY module_id, idx", (course_id,)),
    }


def write_tree(tree: dict, user_id: int, *, template: bool = False, catalog_no: int | None = None, origin_id: int | None = None) -> int:
    """Создать курс из дерева. Прогресс обнуляется: уроки с содержимым — «ready», без — «pending»."""
    c = tree["course"]
    with db.tx():
        cid = db.x("""INSERT INTO courses(user_id,title,description,icon,language,has_practice,practice_kind,status,catalog_no,origin_id,is_template)
                      VALUES(?,?,?,?,?,?,?,'ready',?,?,?)""",
                   (user_id, c["title"], c.get("description", ""), c.get("icon", "📘"), c.get("language", "ru"), int(c.get("has_practice") or 0),
                    c.get("practice_kind", "none"), catalog_no, origin_id, int(template)))
        smap = {}
        for s in tree["sources"]:
            smap[s["id"]] = db.x("INSERT INTO sources(course_id,filename,chars,text) VALUES(?,?,?,?)", (cid, s["filename"], s["chars"], s["text"]))
        for ch in tree["chunks"]:
            db.x("INSERT INTO chunks(course_id,source_id,idx,text,digest) VALUES(?,?,?,?,?)",
                 (cid, smap.get(ch["source_id"], 0), ch["idx"], ch["text"], ch.get("digest")))
        mmap, kept = {}, 0
        for m in tree["modules"]:
            if m.get("kind") == "review":  # модули-повторения — часть личного пути ученика, в копию не берём
                continue
            plan = jl(m.get("plan"), None)
            if isinstance(plan, dict):
                plan.pop("message", None)
                plan.pop("error", None)
            status = "planned" if plan and plan.get("terms") else "new"
            mmap[m["id"]] = db.x("""INSERT INTO modules(course_id,idx,title,summary,objectives,chunk_ids,practical,status,plan,kind)
                                     VALUES(?,?,?,?,?,?,?,?,?,'main')""",
                                 (cid, kept, m["title"], m["summary"], m["objectives"], m["chunk_ids"], m["practical"], status, jd(plan) if plan else None))
            kept += 1
        lmap, rows = {}, []
        for l in tree["lessons"]:
            meta = jl(l.get("meta"), {}) or {}
            if l["module_id"] not in mmap or meta.get("remedial") or meta.get("retry"):
                continue
            status = "ready" if l.get("content") else "pending"
            lid = db.x("INSERT INTO lessons(course_id,module_id,idx,type,title,minutes,status,content,meta) VALUES(?,?,?,?,?,?,?,?,?)",
                       (cid, mmap[l["module_id"]], l["idx"], l["type"], l["title"], l["minutes"], status, l.get("content"), jd(meta)))
            lmap[l["id"]] = lid
            rows.append((lid, meta))
        for lid, meta in rows:  # парные тесты ссылаются на урок-родитель
            if "for" in meta:
                meta["for"] = lmap.get(meta["for"], meta["for"])
                db.x("UPDATE lessons SET meta=? WHERE id=?", (jd(meta), lid))
    return cid


# ----------------------------------------------------------------------- каталог
def _card(c: dict, user_id: int | None) -> dict:
    mins, left, total = gm.course_minutes_left(c["id"])
    mods = db.val("SELECT COUNT(*) FROM modules WHERE course_id=?", (c["id"],), 0)
    out = {"number": c["catalog_no"], "title": c["title"], "description": c["description"], "icon": c["icon"],
           "modules": mods, "lessons": total, "minutes": mins, "has_practice": bool(c["has_practice"]), "added": False, "course_id": None}
    if user_id:
        mine = db.one("SELECT id FROM courses WHERE user_id=? AND origin_id=? ORDER BY id LIMIT 1", (user_id, c["id"]))
        if mine:
            out["added"], out["course_id"] = True, mine["id"]
    return out


def list_catalog(user_id: int | None) -> list[dict]:
    return [_card(c, user_id) for c in db.q("SELECT * FROM courses WHERE is_template=1 AND status='ready' ORDER BY catalog_no")]


def find(number: int) -> dict | None:
    return db.one("SELECT * FROM courses WHERE is_template=1 AND catalog_no=?", (number,))


def add_to_user(number: int, user_id: int) -> dict:
    src = find(number)
    if not src:
        raise KeyError("catalog")
    mine = db.one("SELECT id FROM courses WHERE user_id=? AND origin_id=? ORDER BY id LIMIT 1", (user_id, src["id"]))
    if mine:
        return {"course_id": mine["id"], "already": True}
    cid = write_tree(read_tree(src["id"]), user_id, origin_id=src["id"])
    return {"course_id": cid, "already": False}


def _norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip().lower().replace("ё", "е"))


def fingerprint(course_id: int) -> str:
    """Отпечаток материалов курса: одинаковые файлы (по тексту) дают одинаковый отпечаток."""
    rows = db.q("SELECT text FROM sources WHERE course_id=? ORDER BY id", (course_id,))
    blob = "\n".join(re.sub(r"\s+", " ", r["text"] or "").strip() for r in rows)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest() if blob else ""


def duplicate_of(course_id: int, title: str | None = None) -> dict | None:
    """Найти в каталоге курс, который совпадает с данным: тот же исходный шаблон, те же материалы или то же название."""
    c = db.one("SELECT * FROM courses WHERE id=?", (course_id,))
    if not c:
        return None
    if c["origin_id"]:
        origin = db.one("SELECT * FROM courses WHERE id=? AND is_template=1", (c["origin_id"],))
        if origin:
            return origin
    fp = fingerprint(course_id)
    nt = _norm_title(title or c["title"])
    for t in db.q("SELECT * FROM courses WHERE is_template=1 ORDER BY catalog_no"):
        if t["id"] == course_id:
            continue
        if (fp and fingerprint(t["id"]) == fp) or _norm_title(t["title"]) == nt:
            return t
    return None


def publish(course_id: int, title: str | None = None) -> int:
    """Сделать копию курса общедоступной (с новым номером). Только для администратора. Одинаковые курсы не публикуются."""
    c = db.one("SELECT * FROM courses WHERE id=?", (course_id,))
    if not c or c["is_template"]:
        raise ValueError("Курс нельзя опубликовать")
    dup = duplicate_of(course_id, title)
    if dup:
        raise ValueError(f"Такой курс уже есть в каталоге под номером {dup['catalog_no']} («{dup['title']}»)")
    tree = read_tree(course_id)
    if title:
        tree["course"]["title"] = title
    n = next_number()
    write_tree(tree, system_user_id(), template=True, catalog_no=n)
    return n


def delete_template(number: int):
    """Удалить курс из каталога. Копии, уже добавленные пользователями, остаются у них."""
    t = find(number)
    if not t:
        raise KeyError("catalog")
    delete_course(t["id"])


def dedupe_templates() -> list[int]:
    """Убрать из каталога дубликаты (одинаковые материалы): остаётся курс с меньшим номером. Возвращает удалённые номера."""
    seen: dict[str, dict] = {}
    removed: list[int] = []
    for t in db.q("SELECT * FROM courses WHERE is_template=1 ORDER BY catalog_no"):
        fp = fingerprint(t["id"])
        if not fp:
            continue
        if fp in seen:
            delete_course(t["id"])
            removed.append(t["catalog_no"])
        else:
            seen[fp] = t
    return removed


# ----------------------------------------------------------------------- повторное прохождение
def restart_course(course_id: int):
    """Сбросить прогресс курса и пройти заново. XP, серия и достижения остаются."""
    with db.tx():
        extra = [l["id"] for l in db.q("SELECT id, meta FROM lessons WHERE course_id=?", (course_id,))
                 if (jl(l["meta"], {}) or {}).get("remedial") or (jl(l["meta"], {}) or {}).get("retry")]
        review_mods = [m["id"] for m in db.q("SELECT id FROM modules WHERE course_id=? AND kind='review'", (course_id,))]
        for mid in review_mods:
            for l in db.q("SELECT id FROM lessons WHERE module_id=?", (mid,)):
                extra.append(l["id"])
        for lid in extra:
            db.x("DELETE FROM lessons WHERE id=?", (lid,))
        for mid in review_mods:
            db.x("DELETE FROM modules WHERE id=?", (mid,))
        db.x("DELETE FROM runs WHERE lesson_id IN (SELECT id FROM lessons WHERE course_id=?) OR lesson_id IN (%s)" % (",".join(str(i) for i in extra) or "0"), (course_id,))
        db.x("UPDATE lessons SET status=CASE WHEN content IS NOT NULL THEN 'ready' ELSE 'pending' END, score=NULL, completed_at=NULL, error=NULL WHERE course_id=?", (course_id,))
        db.x("UPDATE modules SET status=CASE WHEN plan IS NOT NULL AND plan NOT LIKE '%\"error\"%' THEN 'planned' ELSE 'new' END, retries=0 WHERE course_id=?", (course_id,))
        # перенумеровать модули (после удаления повторений)
        for i, m in enumerate(db.q("SELECT id FROM modules WHERE course_id=? ORDER BY idx", (course_id,))):
            db.x("UPDATE modules SET idx=? WHERE id=?", (i, m["id"]))
        db.x("UPDATE courses SET status='ready', status_text='', error=NULL WHERE id=?", (course_id,))


# ----------------------------------------------------------------------- удаление курса
def delete_course(course_id: int):
    """Удалить курс со всем содержимым (уроки, ответы, запуски)."""
    with db.tx():
        db.x("DELETE FROM runs WHERE lesson_id IN (SELECT id FROM lessons WHERE course_id=?)", (course_id,))
        for t in ("sources", "chunks", "modules", "lessons", "answers", "activity"):
            db.x(f"DELETE FROM {t} WHERE course_id=?", (course_id,))
        db.x("DELETE FROM courses WHERE id=?", (course_id,))


# ----------------------------------------------------------------------- экспорт / импорт
def export_all() -> dict:
    return {"version": 1, "courses": [dict(read_tree(c["id"]), catalog_no=c["catalog_no"]) for c in
                                      db.q("SELECT id, catalog_no FROM courses WHERE is_template=1 ORDER BY catalog_no")]}


def import_all(data: dict, replace: bool = False) -> dict:
    """Добавить курсы из экспорта. Если номер уже есть: пропустить (replace=False) или заменить курс (replace=True)."""
    added, skipped, replaced = [], [], []
    sysid = system_user_id()
    for t in data.get("courses", []):
        no = t.get("catalog_no")
        if not isinstance(no, int):
            skipped.append(no)
            continue
        old = find(no)
        if old and not replace:
            skipped.append(no)
            continue
        if old:
            delete_course(old["id"])
            replaced.append(no)
        write_tree(t, sysid, template=True, catalog_no=no)
        added.append(no)
    removed = dedupe_templates()  # на случай, если в файле дубликаты уже имеющихся курсов под другими номерами
    added = [n for n in added if n not in removed]
    return {"added": added, "skipped": skipped, "replaced": replaced, "duplicates_removed": removed}
