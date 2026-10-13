"""Авторская подготовка ДОПОЛНЕНИЯ к готовому курсу каталога (новая лекция и т. п.) БЕЗ внешней нейросети.

Пайплайн приложения тот же, что у scripts/author_catalog.py: новый файл → план новых модулей → план каждого модуля → уроки. Каждый запрос к модели
сохраняется в catalog_authoring/<папка>/<ключ>.prompt.txt, а ответ кладётся рядом файлом <ключ>.json (его пишет ассистент по тем же правилам,
что описаны в промпте). Скрипт возобновляемый: запусти → прочитай новые *.prompt.txt → создай *.json → запусти снова.
Когда ответы есть на всё, пакет дополнения сохраняется в файл; его загружает администратор (Настройки → Каталог → «Дополнить курс файлом»):
новые модули добавляются в конец курса и всем, кто уже добавил курс себе, — без потери прогресса.

    python scripts/author_supplement.py <номер курса в каталоге> <файл или файлы с новым материалом> --folder 102_lecture4 --out catalog_supplement_102_4.json

Рабочая база — data/_supplement_work.db (создаётся из catalog_seed.json; можно удалять).
"""
import argparse
import asyncio
import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
os.environ["TELEGRAM_ENABLED"] = "0"
sys.path.insert(0, str(ROOT))

ap = argparse.ArgumentParser()
ap.add_argument("number", type=int, help="номер курса в каталоге (101, 102, …)")
ap.add_argument("files", nargs="+", help="новые материалы (docx, pdf, txt…)")
ap.add_argument("--folder", required=True, help="папка catalog_authoring/<folder> для промптов и ответов")
ap.add_argument("--out", required=True, help="куда сохранить пакет дополнения (json)")
ap.add_argument("--title", default="", help="название дополнения (для администратора)")
args = ap.parse_args()

AUTHORING = ROOT / "catalog_authoring" / args.folder
os.environ["LQ_DB"] = str(ROOT / "data" / "_supplement_work.db")
os.environ["LLM_MANUAL_DIR"] = str(AUTHORING)

from server import catalog, config, generator, ingest, supplement  # noqa: E402
from server.db import db  # noqa: E402

config.FULL_TEXT_LIMIT = 200_000       # для ручной работы показываем полный текст материалов целиком
config.MODULE_CONTEXT_CHARS = 30_000


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def prepare_work_course() -> int:
    """Рабочая копия курса каталога (с готовыми уроками), к которой и добавляется новый материал."""
    seed = ROOT / "catalog_seed.json"
    if not db.val("SELECT COUNT(*) FROM courses WHERE is_template=1", default=0):
        log(f"загружаю каталог из {seed.name}")
        catalog.import_all(json.loads(seed.read_text(encoding="utf-8")))
    tmpl = catalog.find(args.number)
    if not tmpl:
        sys.exit(f"В каталоге нет курса №{args.number}")
    work = db.one("SELECT id FROM courses WHERE origin_id=? AND is_template=0 AND user_id=?", (tmpl["id"], catalog.system_user_id()))
    if work:
        return work["id"]
    return catalog.write_tree(catalog.read_tree(tmpl["id"]), catalog.system_user_id(), origin_id=tmpl["id"])


async def main():
    generator.set_loop(asyncio.get_running_loop())
    cid = prepare_work_course()
    first_new = db.val("SELECT MAX(idx)+1 FROM modules WHERE course_id=? AND kind='main'", (cid,), 0)
    marker = AUTHORING / "_state.json"
    if marker.exists():
        st = json.loads(marker.read_text(encoding="utf-8"))
        module_from, chunk_base, source_ids = st["module_from"], st["chunk_base"], st["source_ids"]
        log(f"продолжаю: новые модули с №{module_from}")
    else:
        module_from = first_new
        chunk_base = db.val("SELECT MAX(idx) FROM chunks WHERE course_id=?", (cid,), 0) or 0
        source_ids = []
    # 1) новый материал и план новых модулей
    if not source_ids:
        for f in args.files:
            p = pathlib.Path(f)
            text = ingest.extract_text(p.name, p.read_bytes())
            source_ids.append(db.x("INSERT INTO sources(course_id,filename,chars,text) VALUES(?,?,?,?)", (cid, p.name, len(text), text)))
        AUTHORING.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"module_from": module_from, "chunk_base": chunk_base, "source_ids": source_ids}), encoding="utf-8")
    if not db.val("SELECT COUNT(*) FROM modules WHERE course_id=? AND idx>=?", (cid, module_from), 0):
        n0 = db.val("SELECT COUNT(*) FROM chunks WHERE course_id=?", (cid,), 0)
        await supplement.supplement_course(cid, source_ids)
        c = db.one("SELECT status, error FROM courses WHERE id=?", (cid,))
        if c["error"] or not db.val("SELECT COUNT(*) FROM modules WHERE course_id=? AND idx>=?", (cid, module_from), 0):
            # ответа на «план дополнения» ещё нет: убираем добавленные фрагменты, чтобы запуск можно было повторить
            db.x("DELETE FROM chunks WHERE course_id=? AND idx>?", (cid, chunk_base))
            db.x("UPDATE courses SET error=NULL, status='ready', status_text='' WHERE id=?", (cid,))
            log(f"нужен ответ на план дополнения → {c['error']}")
            log(f"Промпт: {AUTHORING}{os.sep}")
            return
        log(f"фрагментов было {n0}, стало {db.val('SELECT COUNT(*) FROM chunks WHERE course_id=?', (cid,), 0)}")
    # 2) план и уроки новых модулей
    ok = True
    for m in db.q("SELECT id, idx, title FROM modules WHERE course_id=? AND idx>=? ORDER BY idx", (cid, module_from)):
        try:
            await generator.plan_module(m["id"])
        except Exception as e:
            log(f"   модуль {m['idx']}: {str(e)[:200]}")
        plan = db.val("SELECT plan FROM modules WHERE id=?", (m["id"],))
        err = (json.loads(plan) if plan else {}).get("error")
        if err or not plan:
            log(f"   модуль {m['idx']} «{m['title']}»: нужен ответ → {err}")
            ok = False
            continue
        for l in db.q("SELECT id, type, title FROM lessons WHERE module_id=? ORDER BY idx", (m["id"],)):
            await generator.generate_lesson(l["id"])
            st = db.one("SELECT status, error FROM lessons WHERE id=?", (l["id"],))
            if st["status"] not in ("ready", "done"):
                log(f"   модуль {m['idx']} {l['type']}: нужен ответ → {st['error']}")
                db.x("UPDATE lessons SET status='pending' WHERE id=?", (l["id"],))
                ok = False
                break
        else:
            log(f"   модуль {m['idx']} «{m['title']}»: все уроки готовы")
    if not ok:
        log(f"Нужно ещё ответов. Новые промпты — в {AUTHORING}{os.sep}")
        return
    pkg = catalog.export_supplement(cid, module_from=module_from, chunk_base=chunk_base, source_ids=source_ids, title=args.title)
    pkg["catalog_no"] = args.number
    out = pathlib.Path(args.out)
    out = out if out.is_absolute() else ROOT / out
    out.write_text(json.dumps(pkg, ensure_ascii=False), encoding="utf-8")
    log(f"ГОТОВО. Новых модулей: {len(pkg['modules'])}, уроков: {len(pkg['lessons'])} → {out} ({out.stat().st_size // 1024} КБ)")


asyncio.run(main())
