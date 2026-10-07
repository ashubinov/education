"""Общие операции, которыми пользуются и веб-API, и Telegram-бот."""
from . import auth, config, generator, ingest
from .db import db


def _quota(user_id: int):
    """Лимиты на создание курсов — защита платного ключа модели от случайного или намеренного перерасхода."""
    sysid = db.val("SELECT id FROM users WHERE username=?", (auth.SYSTEM_USERNAME,))
    if user_id == sysid:
        return
    max_total = int(config.env("MAX_COURSES_PER_USER", "12") or 12)
    max_day = int(config.env("MAX_NEW_COURSES_PER_DAY", "4") or 4)
    own = db.val("SELECT COUNT(*) FROM courses WHERE user_id=? AND origin_id IS NULL", (user_id,), 0)
    if own >= max_total:
        raise ValueError(f"Достигнут лимит собственных курсов ({max_total}). Удалите ненужные или возьмите готовые из каталога.")
    today = db.val("SELECT COUNT(*) FROM courses WHERE user_id=? AND origin_id IS NULL AND date(created_at)=date('now')", (user_id,), 0)
    if today >= max_day:
        raise ValueError(f"Сегодня уже создано {max_day} курса — это дневной лимит. Попробуйте завтра.")


def _read_files(files: list[tuple[str, bytes]]) -> tuple[list[tuple[str, str]], list[str]]:
    texts, errors = [], []
    for name, data in files[:20]:
        name = (name or "file.txt").replace("\\", "/").rsplit("/", 1)[-1][:120]
        if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
            errors.append(f"{name}: файл больше {config.MAX_UPLOAD_MB} МБ")
            continue
        try:
            texts.append((name, ingest.extract_text(name, data)))
        except ingest.IngestError as e:
            errors.append(f"{name}: {e}")
        except Exception as e:
            errors.append(f"{name}: не удалось прочитать ({e})")
    if not texts:
        raise ValueError("Не удалось извлечь текст ни из одного файла. " + " ".join(errors))
    return texts, errors


def _limit_chars(texts: list[tuple[str, str]], already: int, errors: list[str]) -> list[tuple[str, str]]:
    total, kept = already, []
    for name, text in texts:
        if total + len(text) > config.MAX_SOURCE_CHARS:
            text = text[: max(0, config.MAX_SOURCE_CHARS - total)]
            errors.append(f"{name}: текст обрезан до лимита {config.MAX_SOURCE_CHARS // 1000}k символов")
        if text.strip():
            kept.append((name, text))
            total += len(text)
    if not kept:
        raise ValueError("Достигнут лимит объёма материалов курса")
    return kept


def create_course(user_id: int, files: list[tuple[str, bytes]], title: str = "") -> tuple[int, list[str]]:
    """Создать курс из загруженных файлов. Возвращает (course_id, предупреждения). Бросает ValueError, если нельзя."""
    _quota(user_id)
    texts, errors = _read_files(files)
    kept = _limit_chars(texts, 0, errors)
    cid = db.x("INSERT INTO courses(user_id,title,status,status_text) VALUES(?,?,?,?)",
               (user_id, (title or "").strip()[:100] or "Новый курс", "processing", "В очереди…"))
    for name, text in kept:
        db.x("INSERT INTO sources(course_id,filename,chars,text) VALUES(?,?,?,?)", (cid, name, len(text), text))
    generator.spawn(generator.build_course(cid))
    return cid, errors


def add_sources(user_id: int, course_id: int, files: list[tuple[str, bytes]]) -> tuple[list[int], list[str]]:
    """Сохранить новые файлы как источники существующего курса. Возвращает (id источников, предупреждения)."""
    texts, errors = _read_files(files)
    already = db.val("SELECT SUM(chars) FROM sources WHERE course_id=?", (course_id,), 0) or 0
    kept = _limit_chars(texts, already, errors)
    ids = [db.x("INSERT INTO sources(course_id,filename,chars,text) VALUES(?,?,?,?)", (course_id, n, len(t), t)) for n, t in kept]
    return ids, errors
