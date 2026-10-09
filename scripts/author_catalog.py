"""Авторская сборка готовых курсов каталога БЕЗ внешней нейросети.

Пайплайн приложения тот же (разбор файлов → структура → план модулей → уроки, те же промпты и проверки ответов), но вместо вызова
модели каждый промпт сохраняется в catalog_authoring/<ключ>.prompt.txt, а ответ кладётся рядом файлом <ключ>.json (его пишет ассистент
по тем же правилам, что описаны в промпте). Скрипт возобновляемый: запусти → прочитай новые *.prompt.txt → создай *.json → запусти снова.
Когда ответы есть на всё, курсы получают номера и экспортируются в catalog_seed.json.

    python scripts/author_catalog.py
"""
import asyncio
import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
AUTHORING = ROOT / "catalog_authoring"
os.environ.setdefault("LQ_DB", str(ROOT / "data" / "catalog_manual.db"))
os.environ["TELEGRAM_ENABLED"] = "0"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from server import catalog, config, generator, service  # noqa: E402
from server.db import db  # noqa: E402

from seed_catalog_lib import COURSES, course_files  # noqa: E402


# для ручной работы показываем полный текст материалов целиком, без сжатия и обрезки
config.FULL_TEXT_LIMIT = 200_000
config.MODULE_CONTEXT_CHARS = 30_000


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


async def work(number: int, folder: str, title: str, missing: list[str]) -> bool:
    os.environ["LLM_MANUAL_DIR"] = str(AUTHORING / str(number))  # ответы каждого курса — в своей папке
    sysid = catalog.system_user_id()
    c = db.one("SELECT * FROM courses WHERE user_id=? AND title=?", (sysid, title))
    if c and c["is_template"]:
        log(f"№{number} «{title}»: уже в каталоге")
        return True
    if not c:
        files = course_files(ROOT / "курсы" / folder)
        log(f"№{number} «{title}»: файлов {len(files)}")
        cid, _ = service.create_course(sysid, [(f.name, f.read_bytes()) for f in files], title)
    else:
        cid = c["id"]
        if c["status"] == "error":
            db.x("UPDATE courses SET status='processing', error=NULL WHERE id=?", (cid,))
            generator.spawn(generator.build_course(cid))
    while True:
        c = db.one("SELECT status, error FROM courses WHERE id=?", (cid,))
        if c["status"] == "ready":
            break
        if c["status"] == "error":
            missing.append(f"{c['error']}")
            log(f"   структура: нужен ответ → {c['error']}")
            return False
        await asyncio.sleep(0.5)
    db.x("UPDATE modules SET plan=NULL WHERE course_id=? AND plan LIKE '%\"error\"%'", (cid,))
    ok = True
    for m in db.q("SELECT id, idx, title FROM modules WHERE course_id=? ORDER BY idx", (cid,)):
        try:
            await generator.plan_module(m["id"])
        except Exception as e:  # нет ответа или ответ не прошёл проверку — модуль пропускаем, ошибка запомнена в плане
            log(f"   модуль {m['idx']}: {str(e)[:200]}")
        plan = db.val("SELECT plan FROM modules WHERE id=?", (m["id"],))
        err = (json.loads(plan) if plan else {}).get("error")
        if err or not plan:
            missing.append(f"модуль {m['idx']}: {err}")
            log(f"   модуль {m['idx']} «{m['title']}»: нужен ответ → {err}")
            ok = False
            continue
        for l in db.q("SELECT id, type, title FROM lessons WHERE module_id=? ORDER BY idx", (m["id"],)):
            await generator.generate_lesson(l["id"])
            st = db.one("SELECT status, error FROM lessons WHERE id=?", (l["id"],))
            if st["status"] not in ("ready", "done"):
                missing.append(f"модуль {m['idx']} урок {l['type']}: {st['error']}")
                log(f"   модуль {m['idx']} {l['type']}: нужен ответ → {st['error']}")
                db.x("UPDATE lessons SET status='pending' WHERE id=?", (l["id"],))
                ok = False
                break
        else:
            log(f"   модуль {m['idx']} «{m['title']}»: все уроки готовы")
    if ok:
        db.x("UPDATE courses SET is_template=1, catalog_no=? WHERE id=?", (number, cid))
        log(f"№{number} «{title}»: ГОТОВО")
    return ok


async def main():
    generator.set_loop(asyncio.get_running_loop())
    missing: list[str] = []
    results = [await work(n, f, t, missing) for n, f, t in COURSES]
    if all(results):
        data = catalog.export_all()
        out = ROOT / "catalog_seed.json"
        out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        log(f"ВСЁ ГОТОВО. Экспортировано курсов: {len(data['courses'])} → {out} ({out.stat().st_size // 1024} КБ)")
    else:
        log(f"Нужно ответов: {len(missing)}. Новые промпты — в {AUTHORING}{os.sep}<номер курса>{os.sep}")


asyncio.run(main())
