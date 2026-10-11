"""Durable, one-way LearnQuest chat event relay to a linked Telegram group.

Reuses the existing tgbot transport/token. NEVER forwards ordinary chat messages.
The local SQLite outbox is populated by triggers in the same transaction that
writes a system/command message; each event is retried until delivered.
Telegram has no sendMessage idempotency key: an uncertain network response can
cause a rare duplicate after recovery (at-least-once delivery).
"""
from __future__ import annotations

import asyncio
import html
import re
import time

from .db import db

GROUP_KEY = "tg_chat_relay_group_id"
OUTBOX_TABLE = "tg_chat_relay_outbox"
_ALLOWED = ('command', 'system', 'strawberry')
# Telegram documents 20 messages/minute in a group; use 1/6 sec with reserve.
GROUP_SEND_INTERVAL = 6.0


class GroupSendCooldown(RuntimeError):
    """Local (no HTTP request) or remote group flood-wait; handled silently by group commands."""
    def __init__(self, seconds: float):
        self.seconds = max(0., float(seconds))
        super().__init__(f'Telegram group rate-limited for {self.seconds:.1f}s')


def _send_key(group_id: int) -> str:
    return f'tg_chat_relay_send_after_{group_id}'


def seconds_until_send(group_id: int, now: float | None = None) -> float:
    """Durable per-group rate and flood wait, surviving bot/site restarts."""
    if now is None:
        now = time.time()
    try:
        return max(0., float(db.get_setting(_send_key(group_id), '0')) - now)
    except (ValueError, TypeError):
        return 0.


def _reserve_group_send(group_id: int, now: float) -> None:
    """Atomically reserve the next slot across processes sharing SQLite."""
    with db.tx():
        wait = seconds_until_send(group_id, now)
        if wait > 0:
            raise GroupSendCooldown(wait)
        db.set_setting(_send_key(group_id), str(now + GROUP_SEND_INTERVAL))


def _block_group_until(group_id: int, deadline: float) -> None:
    with db.tx():
        try:
            old = float(db.get_setting(_send_key(group_id), '0'))
        except (ValueError, TypeError):
            old = 0.
        if deadline > old:
            db.set_setting(_send_key(group_id), str(deadline))


def _retry_after(exc: Exception) -> float:
    """Telegram may report retry_after as either data or text in a RuntimeError."""
    v = getattr(exc, 'retry_after', None)
    if isinstance(v, (int, float)) and v > 0:
        return float(v)
    m = re.search(r'retry\s+after\s+(\d+)', str(exc), re.IGNORECASE)
    return float(m.group(1)) if m else 0.


async def send_group(group_id: int, text: str, buttons=None, **kw):
    """Only path for bot messages into groups (including /relay_status replies)."""
    from . import tgbot
    now = time.time()
    _reserve_group_send(group_id, now)
    params = {'chat_id': group_id, 'text': text[:4000], 'parse_mode': 'HTML',
              'disable_web_page_preview': True}
    if buttons:
        params['reply_markup'] = {'inline_keyboard': [[{'text': t, 'callback_data': d}
                                                        for t, d in row] for row in buttons]}
    params.update(kw)
    try:
        result = await tgbot.api('sendMessage', **params)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        wait = _retry_after(exc)
        if wait:
            # The retry period is an instruction from Telegram, not merely a hint.
            _block_group_until(group_id, time.time() + wait + 3.)
            print(f'[telegram-relay] 429: pausing group {group_id} for {wait:.0f}s')
        raise
    return result


def init_schema():
    """Install idempotent enqueue triggers after chat.init_schema()."""
    with db.lock:
        db.conn.executescript('''
        CREATE TABLE IF NOT EXISTS tg_chat_relay_outbox (
            message_id INTEGER PRIMARY KEY,
            group_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            author TEXT NOT NULL DEFAULT '',
            created_ts INTEGER NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','sending','delivered','skipped')),
            attempts INTEGER NOT NULL DEFAULT 0,
            next_try_ts INTEGER NOT NULL DEFAULT 0,
            lease_until INTEGER NOT NULL DEFAULT 0,
            telegram_message_id INTEGER,
            delivered_ts INTEGER,
            last_error TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_tg_chat_relay_send
          ON tg_chat_relay_outbox(state,next_try_ts,message_id);
        CREATE TRIGGER IF NOT EXISTS tr_tg_relay_chat_insert
        AFTER INSERT ON chat_messages
        WHEN NEW.system_kind IN ('command','system','strawberry')
        BEGIN
          INSERT OR IGNORE INTO tg_chat_relay_outbox
              (message_id,group_id,kind,content,author,created_ts)
          SELECT NEW.id, CAST(s.value AS INTEGER), NEW.system_kind, NEW.text,
              COALESCE((SELECT COALESCE(NULLIF(TRIM(display_name),''),username)
                        FROM users WHERE id=NEW.user_id),''), NEW.created_ts
            FROM settings AS s
           WHERE s.key='tg_chat_relay_group_id' AND s.value GLOB '-[0-9]*';
        END;
        CREATE TRIGGER IF NOT EXISTS tr_tg_relay_chat_kind
        AFTER UPDATE OF system_kind ON chat_messages
        WHEN NEW.system_kind IN ('command','system','strawberry')
        BEGIN
          INSERT OR IGNORE INTO tg_chat_relay_outbox
              (message_id,group_id,kind,content,author,created_ts)
          SELECT NEW.id, CAST(s.value AS INTEGER), NEW.system_kind, NEW.text,
              COALESCE((SELECT COALESCE(NULLIF(TRIM(display_name),''),username)
                        FROM users WHERE id=NEW.user_id),''), NEW.created_ts
            FROM settings AS s
           WHERE s.key='tg_chat_relay_group_id' AND s.value GLOB '-[0-9]*';
        END;
        ''')


def target_group() -> int | None:
    raw = db.get_setting(GROUP_KEY, '').strip()
    try:
        n = int(raw)
    except (ValueError, TypeError):
        return None
    return n if n < 0 else None


def configure_group(group_id: int | None) -> None:
    """Switch output exclusively; abandon unsent events for old destination."""
    if group_id is not None and (not isinstance(group_id, int) or group_id >= 0):
        raise ValueError('The target must be a Telegram group/supergroup ID')
    with db.tx():
        old = target_group()
        if old == group_id:
            return
        if old is not None:
            db.x("UPDATE tg_chat_relay_outbox SET state='skipped', last_error='destination changed' "
                 "WHERE group_id=? AND state IN ('pending','sending')", (old,))
        db.set_setting(GROUP_KEY, str(group_id) if group_id is not None else '')


def _format(entry: dict) -> str:
    body = html.escape(entry['content'])
    if entry['kind'] == 'command':
        author = html.escape(entry['author'] or 'Пользователь')
        return f"🎁 <b>КОМАНДА</b> · {author}\n{body}"
    if entry['kind'] == 'strawberry':
        return f"🍓 <b>КЛУБНИЧНЫЙ ВОР</b>\n{body}"
    return f"📢 <b>LearnQuest</b>\n{body}"


def _take(now: int) -> dict | None:
    with db.tx():
        group = target_group()
        if group is None or seconds_until_send(group) > 0:
            return None
        # A crashed worker can abandon a lease; a later run safely retries.
        row = db.one('''SELECT * FROM tg_chat_relay_outbox
           WHERE group_id=? AND ((state='pending' AND next_try_ts<=?) OR
               (state='sending' AND lease_until<=?))
           ORDER BY message_id LIMIT 1''', (group, now, now))
        if not row:
            return None
        db.x('''UPDATE tg_chat_relay_outbox SET state='sending',lease_until=?,attempts=attempts+1
                 WHERE message_id=?''', (now + 100, row['message_id']))
        return row


def _done(msg_id: int, tg_msg_id: int | None, group_id: int, now: int) -> None:
    with db.tx():
        if target_group() == group_id:
            db.x('''UPDATE tg_chat_relay_outbox
                SET state='delivered',telegram_message_id=?,delivered_ts=?,lease_until=0,last_error=NULL
                WHERE message_id=? AND state='sending' AND group_id=?''',
                (tg_msg_id, now, msg_id, group_id))


def _retry(row: dict, exc: Exception, now: int) -> None:
    # Honor Telegram's full retry_after: never cap it to our normal 15-min backoff.
    wait = max(min(900, 2 ** min(int(row['attempts']) + 1, 9)),
               int(_retry_after(exc) + 4),
               int(seconds_until_send(row['group_id'])) + 2)
    with db.tx():
        db.x('''UPDATE tg_chat_relay_outbox
            SET state='pending',lease_until=0,next_try_ts=?,last_error=?
            WHERE message_id=? AND state='sending' AND group_id=?''',
            (now + wait, str(exc)[:250], row['message_id'], row['group_id']))


async def run_once() -> bool:
    """Attempt one queued message. Returns True only when work was claimed."""
    from . import tgbot
    if not tgbot.token():
        return False
    row = _take(int(time.time()))
    if row is None:
        return False
    try:
        result = await tgbot.send(row['group_id'], _format(row), disable_notification=True)
    except asyncio.CancelledError:
        # Lease expires and a restarted worker retries.
        raise
    except Exception as exc:
        _retry(row, exc, int(time.time()))
    else:
        _done(row['message_id'], result.get('message_id') if isinstance(result, dict) else None,
              row['group_id'], int(time.time()))
    return True


async def worker_loop():
    """Started/stopped by the FastAPI lifespan, NOT a second Telegram poller."""
    while True:
        try:
            did_work = await run_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print('[telegram-relay] worker:', repr(exc))
            await asyncio.sleep(8)
            continue
        await asyncio.sleep(0.4 if did_work else 2.5)


def linked_admin(telegram_user_id: int | None) -> bool:
    """Only previously linked, active LearnQuest admins may configure forwarding."""
    if not isinstance(telegram_user_id, int) or telegram_user_id <= 0:
        return False
    row = db.one('''SELECT id FROM users WHERE tg_chat_id=? AND is_admin=1
                    AND COALESCE(banned,0)=0''', (telegram_user_id,))
    return bool(row)


async def telegram_group_admin(group_id: int, telegram_user_id: int) -> bool:
    from . import tgbot
    try:
        member = await tgbot.api('getChatMember', chat_id=group_id, user_id=telegram_user_id)
        return member.get('status') in ('creator', 'administrator')
    except Exception:
        return False


async def register_if_authorized(group_id: int, user_id: int) -> bool:
    if not linked_admin(user_id) or not await telegram_group_admin(group_id, user_id):
        return False
    configure_group(group_id)
    return True
