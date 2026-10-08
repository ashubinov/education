"""Создаёт готовые курсы каталога из папки «курсы/» и экспортирует их в catalog_seed.json.

Каждый курс собирается ИИ, затем ПОЛНОСТЬЮ предгенерируется (план каждого модуля и все уроки), чтобы друзья, которые
добавляют курс по номеру, не тратили запросы к модели. Скрипт возобновляемый: при ошибке/лимите API просто запусти его снова —
готовые части повторно не генерируются. Работает с отдельной базой data/catalog_work.db.

    python scripts/seed_catalog.py            # все курсы
    python scripts/seed_catalog.py --only 102 # только курс с номером 102
"""
import asyncio
import os
import pathlib
import re
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
os.environ.setdefault("LQ_DB", str(ROOT / "data" / "catalog_work.db"))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")  # бот в этом процессе не нужен
os.environ["TELEGRAM_ENABLED"] = "0"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from server import catalog, generator, service, supplement  # noqa: E402
from server.db import db, jl  # noqa: E402
from server.llm import llm  # noqa: E402

from seed_catalog_lib import COURSES, course_files  # noqa: E402


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


async def build(number: int, folder: str, title: str):
    sysid = catalog.system_user_id()
    c = db.one("SELECT * FROM courses WHERE user_id=? AND title=?", (sysid, title))
    if c and c["is_template"]:
        log(f"№{number} «{title}»: уже в каталоге, пропускаю")
        return
    files = course_files(ROOT / "курсы" / folder)
    log(f"№{number} «{title}»: файлов {len(files)}: " + ", ".join(f.name for f in files))
    if not c:
        cid, warns = service.create_course(sysid, [(f.name, f.read_bytes()) for f in files], title)
        for w in warns:
            log("   предупреждение: " + w)
    else:
        cid = c["id"]
        if c["status"] == "error":
            db.x("UPDATE courses SET status='processing', error=NULL WHERE id=?", (cid,))
            generator.spawn(generator.build_course(cid))
    while True:  # ждём структуру курса
        c = db.one("SELECT status, status_text, error FROM courses WHERE id=?", (cid,))
        if c["status"] == "ready":
            break
        if c["status"] == "error":
            raise RuntimeError(c["error"])
        await asyncio.sleep(2)
    mods = db.q("SELECT title FROM modules WHERE course_id=? ORDER BY idx", (cid,))
    log(f"   структура готова: {len(mods)} модулей: " + "; ".join(m["title"] for m in mods))
    db.x("UPDATE modules SET plan=NULL WHERE course_id=? AND plan LIKE '%\"error\"%'", (cid,))
    await supplement.pregenerate_course(cid, log=log)
    db.x("UPDATE courses SET is_template=1, catalog_no=? WHERE id=?", (number, cid))
    log(f"№{number} «{title}»: ГОТОВО")


async def main():
    only = int(sys.argv[sys.argv.index("--only") + 1]) if "--only" in sys.argv else None
    generator.set_loop(asyncio.get_running_loop())
    chain = await llm.chain()
    log("цепочка моделей: " + ", ".join(m for _, m in chain))
    failed = []
    for number, folder, title in COURSES:
        if only and number != only:
            continue
        try:
            await build(number, folder, title)
        except Exception as e:
            log(f"№{number} «{title}»: ОШИБКА — {e}")
            failed.append(number)
    out = ROOT / "catalog_seed.json"
    import json
    data = catalog.export_all()
    out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    log(f"экспортировано курсов: {len(data['courses'])} -> {out} ({out.stat().st_size // 1024} КБ)")
    if failed:
        log("Не готовы курсы: " + ", ".join(map(str, failed)) + ". Запусти скрипт ещё раз (позже или после пополнения баланса DeepSeek).")
        sys.exit(1)


asyncio.run(main())
