"""Резервные копии базы: раз в сутки снимок SQLite (сжатый) в DATA/backups, хранятся последние BACKUP_KEEP штук.

Снимок делается штатным механизмом SQLite (backup API) — он согласован даже во время работы сервера.
Администратор видит список и может скачать копию к себе (Настройки → Резервные копии): копии на самом сервере не спасут,
если пропадёт весь сервер, поэтому время от времени сохраняй свежую копию на свой компьютер.
В копии есть всё: пользователи, курсы, прогресс, ключи и токены из настроек — храни файл так же аккуратно, как пароли.
"""
import asyncio
import gzip
import os
import pathlib
import re
import shutil
import sqlite3
import time
from datetime import datetime

from . import config
from .db import db

DIR = config.DATA / "backups"
NAME_RE = re.compile(r"^learnquest-[0-9_-]+\.db\.gz$")
EVERY_SECONDS = 24 * 3600
_task: asyncio.Task | None = None


def keep() -> int:
    try:
        return max(1, int(config.env("BACKUP_KEEP", "7")))
    except ValueError:
        return 7


def _info(p: pathlib.Path) -> dict:
    st = p.stat()
    return {"name": p.name, "size": st.st_size, "created": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")}


def list_backups() -> list[dict]:
    if not DIR.exists():
        return []
    return [_info(p) for p in sorted(DIR.glob("learnquest-*.db.gz"), reverse=True) if NAME_RE.match(p.name)]


def path_of(name: str) -> pathlib.Path | None:
    p = DIR / name
    return p if NAME_RE.match(name) and p.is_file() else None


def make_backup() -> dict:
    DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")
    raw, final = DIR / f"learnquest-{stamp}.db", DIR / f"learnquest-{stamp}.db.gz"
    try:
        with db.lock:  # пока идёт снимок, остальные запросы к базе ждут (база небольшая — доли секунды)
            dst = sqlite3.connect(str(raw))
            try:
                db.conn.backup(dst)
            finally:
                dst.close()
        with open(raw, "rb") as f_in, gzip.open(str(final) + ".part", "wb", compresslevel=6) as f_out:
            shutil.copyfileobj(f_in, f_out)
        os.replace(str(final) + ".part", final)
    finally:
        for junk in (raw, pathlib.Path(str(final) + ".part")):
            try:
                junk.unlink()
            except OSError:
                pass
    prune()
    return _info(final)


def prune():
    files = sorted((p for p in DIR.glob("learnquest-*.db.gz") if NAME_RE.match(p.name)), reverse=True)
    for old in files[keep():]:
        try:
            old.unlink()
        except OSError:
            pass


def _last_age() -> float | None:
    files = list_backups()
    if not files:
        return None
    return time.time() - datetime.strptime(files[0]["created"], "%Y-%m-%d %H:%M:%S").timestamp()


async def _loop():
    await asyncio.sleep(30)  # дать серверу спокойно запуститься
    while True:
        try:
            age = _last_age()
            if age is None or age >= EVERY_SECONDS:
                info = await asyncio.to_thread(make_backup)
                print("Резервная копия базы:", info["name"], info["size"] // 1024, "КБ")
        except Exception as e:  # noqa: BLE001 — сбой копии не должен ронять сервер
            print("Не удалось сделать резервную копию:", e)
        await asyncio.sleep(3600)


def start():
    global _task
    if config.env("BACKUP_ENABLED", "1").lower() in ("0", "false", "no"):
        return
    _task = asyncio.create_task(_loop())


async def stop():
    if _task:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
