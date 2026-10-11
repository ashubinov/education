#!/usr/bin/env python3
"""Read-only diagnostic. Does not contact Telegram or restart bot."""
from __future__ import annotations
from pathlib import Path
import sqlite3
import time
from server import config

p=Path(config.DB_PATH)
if not p.exists():
    print('База данных не найдена:',p)
    raise SystemExit(1)
with sqlite3.connect(str(p),timeout=10) as c:
    c.row_factory=sqlite3.Row
    try:
        grp=c.execute("SELECT value FROM settings WHERE key='tg_chat_relay_group_id'").fetchone()
        group=grp['value'] if grp and grp['value'] else None
        print('Группа Telegram:',group or 'не привязана')
        if group:
            row=c.execute('SELECT value FROM settings WHERE key=?',(f'tg_chat_relay_send_after_{group}',)).fetchone()
            try:
                seconds=max(0,int(float(row['value'])-time.time())) if row else 0
            except (TypeError,ValueError):
                seconds=0
            print('Осталось до возможной отправки:',seconds,'сек')
        exists=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tg_chat_relay_outbox'").fetchone()
        if exists:
            for r in c.execute('SELECT state,COUNT(*) AS n FROM tg_chat_relay_outbox GROUP BY state ORDER BY state'):
                print('Очередь:',r['state'],r['n'])
            for r in c.execute("SELECT message_id,last_error FROM tg_chat_relay_outbox WHERE last_error IS NOT NULL ORDER BY message_id DESC LIMIT 2"):
                print('Последняя ошибка сообщения',r['message_id'],':',r['last_error'])
    except sqlite3.DatabaseError as e:
        print('Ошибка чтения настроек:',e)
        raise SystemExit(1)
