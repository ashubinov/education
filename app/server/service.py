"""Общие операции, которыми пользуются и веб-API, и Telegram-бот."""
from . import config, generator, ingest
from .db import db


def create_course(user_id: int, files: list[tuple[str, bytes]], title: str = "") -> tuple[int, list[str]]:
    """Создать курс из загруженных файлов. Возвращает (course_id, предупреждения). Бросает ValueError, если текста нет."""
    texts, errors = [], []
    for name, data in files[:20]:
        if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
            errors.append(f"{name}: файл больше {config.MAX_UPLOAD_MB} МБ")
            continue
        try:
            texts.append((name or "file.txt", ingest.extract_text(name or "file.txt", data)))
        except ingest.IngestError as e:
            errors.append(f"{name}: {e}")
        except Exception as e:
            errors.append(f"{name}: не удалось прочитать ({e})")
    if not texts:
        raise ValueError("Не удалось извлечь текст ни из одного файла. " + " ".join(errors))
    total, kept = 0, []
    for name, text in texts:
        if total + len(text) > config.MAX_SOURCE_CHARS:
            text = text[: max(0, config.MAX_SOURCE_CHARS - total)]
            errors.append(f"{name}: текст обрезан до лимита {config.MAX_SOURCE_CHARS // 1000}k символов")
        if text.strip():
            kept.append((name, text))
            total += len(text)
    cid = db.x("INSERT INTO courses(user_id,title,status,status_text) VALUES(?,?,?,?)",
               (user_id, (title or "").strip()[:100] or "Новый курс", "processing", "В очереди…"))
    for name, text in kept:
        db.x("INSERT INTO sources(course_id,filename,chars,text) VALUES(?,?,?,?)", (cid, name, len(text), text))
    generator.spawn(generator.build_course(cid))
    return cid, errors
