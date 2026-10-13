"""Live, server-authoritative pet PvP with transactional escrow and turn betting.

Two players place virtual LearnQuest chips (never real money).  Every chip movement
is committed in the same SQLite transaction as the match transition and ledger.
Supports one Uvicorn worker: websocket fan-out is deliberately process-local.
"""
from __future__ import annotations

import asyncio
import json
import string
import secrets
import threading
import time
from collections import defaultdict

import jwt
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from . import auth, config, pet, slots, social
from . import gamification as gm
from .db import db

router = APIRouter(prefix="/api/pet/pvp", tags=["Pet PvP"])
RNG = secrets.SystemRandom()
MIN_BET, MAX_BET, STEP = 10, 200, 10
MAX_COMMITTED = 2000  # per player, over the entire match
MAX_RAISES = 3  # per betting phase
MIN_FULLNESS = 20
INVITE_SECONDS, TURN_SECONDS, COMBAT_SECONDS = 120, 35, 15
BASE_HP = 120.0
QTE_SECONDS = 4.2
ATTACK_INTERVAL = 0.32
GUARD_INTERVAL = 1.5
DAILY_LIMIT, PAIR_LIMIT = 5, 2
MAX_BALANCE = 9_000_000_000_000_000
ACTIVE = ("invited", "betting", "reveal")
FINISHED = ("finished", "cancelled")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InviteIn(Strict):
    defender_id: int = Field(ge=1, le=10**9, strict=True)
    bet: int = Field(ge=MIN_BET, le=MAX_BET, strict=True)
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,64}$")


class MatchIn(Strict):
    match_id: int = Field(ge=1, strict=True)
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,64}$")


class ActionIn(MatchIn):
    move: str = Field(pattern=r"^(check|call|raise|fold)$")
    amount: int = Field(default=0, ge=0, le=MAX_BET, strict=True)


class CombatIn(MatchIn):
    move: str = Field(pattern=r"^(attack|guard)$")


class QteIn(MatchIn):
    nonce: int = Field(ge=1, strict=True)
    sequence: str = Field(pattern=r"^[A-Z]{1,3}$")


def init_schema():
    with db.lock:
        db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS pvp_matches(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          challenger_id INTEGER NOT NULL, defender_id INTEGER NOT NULL,
          challenger_name TEXT NOT NULL, defender_name TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('invited','betting','reveal','finished','cancelled')),
          initial_bet INTEGER NOT NULL, pot INTEGER NOT NULL,
          total_a INTEGER NOT NULL, total_b INTEGER NOT NULL,
          street_a INTEGER NOT NULL DEFAULT 0, street_b INTEGER NOT NULL DEFAULT 0,
          round_no INTEGER NOT NULL DEFAULT 1, wins_a INTEGER NOT NULL DEFAULT 0, wins_b INTEGER NOT NULL DEFAULT 0,
          rounds_json TEXT NOT NULL DEFAULT '[]', turn_user INTEGER,
          checks INTEGER NOT NULL DEFAULT 0, raises INTEGER NOT NULL DEFAULT 0,
          power_a REAL, power_b REAL, pet_a TEXT, pet_b TEXT,
          winner_id INTEGER, result_reason TEXT, deadline REAL NOT NULL,
          created_at REAL NOT NULL, ended_at REAL,
          CHECK(challenger_id <> defender_id),
          CHECK(pot = total_a + total_b)
        );
        CREATE INDEX IF NOT EXISTS ix_pvp_a ON pvp_matches(challenger_id,id DESC);
        CREATE INDEX IF NOT EXISTS ix_pvp_b ON pvp_matches(defender_id,id DESC);
        CREATE INDEX IF NOT EXISTS ix_pvp_deadline ON pvp_matches(status,deadline);
        CREATE TABLE IF NOT EXISTS pvp_requests(
          user_id INTEGER NOT NULL, request_id TEXT NOT NULL,
          match_id INTEGER NOT NULL, action TEXT NOT NULL,
          PRIMARY KEY(user_id,request_id)
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS pvp_ledger(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          match_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
          delta INTEGER NOT NULL, kind TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_pvp_ledger_match ON pvp_ledger(match_id,id);
        CREATE TABLE IF NOT EXISTS pvp_combat(
          match_id INTEGER PRIMARY KEY,
          hp_a REAL NOT NULL DEFAULT 120, hp_b REAL NOT NULL DEFAULT 120,
          energy_a REAL NOT NULL DEFAULT 100, energy_b REAL NOT NULL DEFAULT 100,
          attack_a REAL NOT NULL DEFAULT 0, attack_b REAL NOT NULL DEFAULT 0,
          guard_a REAL NOT NULL DEFAULT 0, guard_b REAL NOT NULL DEFAULT 0,
          energy_at REAL NOT NULL DEFAULT 0,
          tick_at REAL NOT NULL DEFAULT 0,
          events_json TEXT NOT NULL DEFAULT '[]', event_seq INTEGER NOT NULL DEFAULT 0,
          phase_until REAL NOT NULL DEFAULT 0,
          qte_nonce INTEGER NOT NULL DEFAULT 0,
          qte_a TEXT NOT NULL DEFAULT '', qte_b TEXT NOT NULL DEFAULT '',
          qte_done_a INTEGER NOT NULL DEFAULT 0, qte_done_b INTEGER NOT NULL DEFAULT 0,
          qte_until REAL NOT NULL DEFAULT 0, next_qte REAL NOT NULL DEFAULT 0,
          qte_count INTEGER NOT NULL DEFAULT 0
        );
        """)
        # Upgrade from the previous PvP release without losing in-progress bets.
        # The old release has no pvp_combat row for accepted battles.
        ts = time.time()
        db.conn.execute("""INSERT OR IGNORE INTO pvp_combat(match_id,energy_at,tick_at,phase_until,next_qte)
            SELECT id,?,?,CASE WHEN status='reveal' THEN deadline ELSE 0 END,?
            FROM pvp_matches WHERE status IN ('betting','reveal')""", (ts, ts, ts + 4.0))
        db.conn.commit()


# -------------------------- websocket hub (one process) ----------------------
_loop: asyncio.AbstractEventLoop | None = None
_clients: dict[int, set[WebSocket]] = defaultdict(set)
_client_lock = threading.RLock()


def set_loop(loop: asyncio.AbstractEventLoop):
    global _loop
    _loop = loop


def is_online(user_id: int) -> bool:
    with _client_lock:
        return bool(_clients.get(user_id))


async def _send_event(ids: set[int]):
    with _client_lock:
        sockets = [ws for uid in ids for ws in _clients.get(uid, ())]
    for ws in sockets:
        try:
            await ws.send_json({"type": "pvp_change"})
        except Exception:
            pass  # receive loop will unregister dead sockets


def notify(*ids: int):
    if _loop and _loop.is_running():
        _loop.call_soon_threadsafe(lambda: asyncio.create_task(_send_event(set(ids))))


def _notify_presence(uid: int):
    friends = db.q("""SELECT CASE WHEN requester_id=? THEN addressee_id ELSE requester_id END AS id
       FROM friendships WHERE status='accepted' AND (requester_id=? OR addressee_id=?)""", (uid, uid, uid))
    notify(uid, *(f["id"] for f in friends))


def _ws_identity(token: str):
    try:
        claims = jwt.decode(token, auth.secret(), algorithms=["HS256"], options={"require": ["exp", "sub"]})
        uid = int(claims["sub"])
        user = db.one("SELECT * FROM users WHERE id=?", (uid,))
        if not user or user["username"] == auth.SYSTEM_USERNAME or user.get("banned") or user.get("must_change"):
            return None
        if (user["token_version"] or 0) != claims.get("tv", 0):
            return None
        return user
    except (jwt.PyJWTError, ValueError, KeyError, TypeError):
        return None


@router.websocket("/ws")
async def websocket_pvp(ws: WebSocket):
    # CORS middleware does not enforce WebSocket Origin; check it here explicitly.
    origin = ws.headers.get("origin", "")
    allowed = {x.strip().rstrip("/") for x in config.env("CORS_ORIGINS").split(",") if x.strip()}
    allowed |= {f"http://localhost:{config.PORT}", f"http://127.0.0.1:{config.PORT}"}
    if origin and origin.rstrip("/") not in allowed:
        await ws.close(code=1008)
        return
    await ws.accept()
    uid = None
    try:
        raw = await asyncio.wait_for(ws.receive_json(), timeout=10)
        if not isinstance(raw, dict) or raw.get("type") != "auth" or not isinstance(raw.get("token"), str):
            await ws.close(code=1008)
            return
        user = await run_in_threadpool(_ws_identity, raw["token"])
        if not user:
            await ws.close(code=1008)
            return
        uid = user["id"]
        with _client_lock:
            _clients[uid].add(ws)
        await ws.send_json({"type": "pvp_ready"})
        await run_in_threadpool(_notify_presence, uid)
        while True:
            msg = await ws.receive_json()
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "ping":
                # Revalidate account credentials on heartbeats, not just at handshake.
                if not await run_in_threadpool(_ws_identity, raw["token"]):
                    await ws.close(code=1008)
                    return
                await ws.send_json({"type": "pong"})
    except (WebSocketDisconnect, asyncio.TimeoutError, ValueError, RuntimeError):
        pass
    finally:
        if uid is not None:
            with _client_lock:
                _clients[uid].discard(ws)
                if not _clients[uid]:
                    del _clients[uid]
            await run_in_threadpool(_notify_presence, uid)


# -------------------------- finances ----------------------------------------
def _ledger(mid: int, uid: int, delta: int, kind: str, at: float):
    db.x("INSERT INTO pvp_ledger(match_id,user_id,delta,kind,created_at) VALUES(?,?,?,?,?)", (mid, uid, delta, kind, at))


def _debit(mid: int, uid: int, amount: int, kind: str, at: float):
    assert amount > 0
    acc = slots.ensure_account(uid)
    if acc["balance"] < amount:
        raise HTTPException(400, "Недостаточно жетонов")
    db.x("UPDATE slot_accounts SET balance=balance-?,updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (amount, uid))
    _ledger(mid, uid, -amount, kind, at)


def _credit(mid: int, uid: int, amount: int, kind: str, at: float):
    if amount <= 0:
        return
    acc = slots.ensure_account(uid)
    if acc["balance"] > MAX_BALANCE - amount:
        raise HTTPException(409, "Превышен предел баланса")
    db.x("UPDATE slot_accounts SET balance=balance+?,updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (amount, uid))
    _ledger(mid, uid, amount, kind, at)


def _row(mid: int):
    return db.one("SELECT * FROM pvp_matches WHERE id=?", (mid,))


def _mine(m: dict, uid: int) -> bool:
    return uid in (m["challenger_id"], m["defender_id"])


def _other(m: dict, uid: int) -> int:
    return m["defender_id"] if uid == m["challenger_id"] else m["challenger_id"]


def _active_match(uid: int):
    return db.one("""SELECT * FROM pvp_matches
      WHERE (challenger_id=? OR defender_id=?) AND status IN ('invited','betting','reveal') ORDER BY id DESC LIMIT 1""", (uid, uid))


def ensure_no_active(uid: int):
    if _active_match(uid):
        raise HTTPException(409, "Питомец участвует в PvP. Заверши бой или отклони приглашение")


def _limits(uid: int, oid: int, now: float):
    # Excludes still-open matches, but both players may have only one active match.
    # gm.today() uses the machine-local date, not a fixed geographic timezone.
    from datetime import datetime
    start_of_day = gm.today()
    def today_count(rows):
        return sum(datetime.fromtimestamp(r["ended_at"]).date().isoformat() == start_of_day for r in rows)
    rows = db.q("""SELECT challenger_id,defender_id,ended_at FROM pvp_matches
      WHERE (challenger_id=? OR defender_id=?) AND status='finished' AND ended_at>=?""", (uid, uid, now - 172800))
    if today_count(rows) >= DAILY_LIMIT:
        raise HTTPException(400, "Лимит PvP-боёв на сегодня исчерпан")
    if today_count([r for r in rows if oid in (r["challenger_id"], r["defender_id"])]) >= PAIR_LIMIT:
        raise HTTPException(400, "С этим другом сегодня уже было два PvP-боя")


def _eligible(uid: int, now: float):
    r = pet._get(uid)
    if not r:
        raise HTTPException(400, "Для PvP нужен питомец")
    if pet._fullness(r, now) < MIN_FULLNESS:
        raise HTTPException(400, "Питомец слишком голоден: покорми его")
    return r


def _power(r: dict, now: float) -> float:
    return round((1 + r["xp"] / 300) * (0.6 + 0.4 * pet._fullness(r, now) / 100), 4)


def win_probability(a: float, b: float) -> float:
    """Per-round probability; best-of-three magnifies the strength advantage."""
    x, y = max(0.01, a)**1.4, max(0.01, b)**1.4
    return max(0.1, min(0.9, x / (x + y)))


def _request(uid: int, request_id: str, match_id: int, action: str):
    old = db.one("SELECT * FROM pvp_requests WHERE user_id=? AND request_id=?", (uid, request_id))
    if old:
        if old["action"] != action or (match_id and old["match_id"] != match_id):
            raise HTTPException(409, "request_id уже использован для другого действия")
        return old
    return None


def _record_request(uid: int, rid: str, mid: int, action: str):
    db.x("INSERT INTO pvp_requests(user_id,request_id,match_id,action) VALUES(?,?,?,?)", (uid, rid, mid, action))


def _finish(m: dict, winner: int, reason: str, now: float):
    assert m["status"] in ("betting", "reveal")
    _credit(m["id"], winner, m["pot"], "payout_" + reason, now)
    db.x("""UPDATE pvp_matches SET status='finished',winner_id=?,result_reason=?,deadline=?,ended_at=?,turn_user=NULL
       WHERE id=?""", (winner, reason, now, now, m["id"]))
    # Spend satiety and award XP to *both* fighters, unlike the legacy arena.
    for uid in (m["challenger_id"], m["defender_id"]):
        r = pet._get(uid)
        if r:
            f = pet._fullness(r, now)
            db.x("UPDATE pets SET fullness=?,fed_at=?,xp=xp+? WHERE user_id=?", (max(0, f - 12), now, 15 if uid == winner else 5, uid))


def _cancel(m: dict, reason: str, now: float):
    assert m["status"] == "invited"
    _credit(m["id"], m["challenger_id"], m["pot"], "invite_refund", now)
    db.x("UPDATE pvp_matches SET status='cancelled',result_reason=?,deadline=?,ended_at=? WHERE id=?", (reason, now, now, m["id"]))


# -------------------------- interactive combat -------------------------------
def _combat(mid: int):
    return db.one("SELECT * FROM pvp_combat WHERE match_id=?", (mid,))


def _event(c: dict, kind: str, now: float, **details):
    events = json.loads(c["events_json"])
    seq = c["event_seq"] + 1
    events.append({"seq": seq, "type": kind, "at": round(now, 3), **details})
    db.x("UPDATE pvp_combat SET events_json=?,event_seq=? WHERE match_id=?",
         (json.dumps(events[-45:], ensure_ascii=False), seq, c["match_id"]))
    return _combat(c["match_id"])


def _damage(m: dict, attacker: int, kind: str, now: float, multiplier: float = 1.0):
    c = _combat(m["id"])
    enemy = _other(m, attacker)
    is_a = attacker == m["challenger_id"]
    target_key = "hp_b" if is_a else "hp_a"
    their_guard = c["guard_b" if is_a else "guard_a"]
    strength = m["power_a"] if is_a else m["power_b"]
    opposing = m["power_b"] if is_a else m["power_a"]
    # Equal inputs: strength dominates, small bounded randomness is retained.
    ratio = max(.22, min(3.0, strength / max(.01, opposing))) ** .72
    # No passive attacks: damage is possible only from explicit attack or solved QTE.
    if kind not in ("attack", "critical"):
        raise ValueError("Нельзя наносить пассивный урон")
    base = 3.5 if kind == "attack" else 14.0
    damage = round(base * ratio * RNG.uniform(.86, 1.14) * multiplier * (.38 if their_guard > now else 1.0), 1)
    hp = max(0.0, round(c[target_key] - damage, 1))
    db.x(f"UPDATE pvp_combat SET {target_key}=? WHERE match_id=?", (hp, m["id"]))
    c = _event(_combat(m["id"]), "hit", now, source=attacker, target=enemy,
               damage=damage, attack=kind, hp=hp, guarded=their_guard > now)
    if hp <= 0:
        _event(c, "knockout", now, winner_id=attacker)
        _finish(m, attacker, "knockout", now)
        return True
    return False


def _start_combat(m: dict, now: float):
    c = _combat(m["id"])
    if not c:
        raise RuntimeError("Combat state missing")
    until = now + COMBAT_SECONDS
    db.x("""UPDATE pvp_combat SET energy_a=100,energy_b=100,energy_at=?,tick_at=?,
        attack_a=0,attack_b=0,guard_a=0,guard_b=0,phase_until=?,
        qte_a='',qte_b='',qte_until=0,next_qte=?,qte_count=0,
        qte_done_a=0,qte_done_b=0 WHERE match_id=?""",
         (now, now, until, now + RNG.uniform(3.5, 5.5), m["id"]))
    db.x("UPDATE pvp_matches SET status='reveal',turn_user=NULL,deadline=? WHERE id=?", (until, m["id"]))
    _event(_combat(m["id"]), "phase", now, round=m["round_no"])


def _round(m: dict, now: float):
    # Poker betting is complete. Open an interactive combat phase instead of
    # rolling an immediate best-of-three winner.
    _start_combat(m, now)


def _regen(c: dict, now: float):
    dt = max(0.0, min(15.0, now - c["energy_at"]))
    if dt < .01:
        return c
    a, b = min(100., c["energy_a"] + 12 * dt), min(100., c["energy_b"] + 12 * dt)
    db.x("UPDATE pvp_combat SET energy_a=?,energy_b=?,energy_at=? WHERE match_id=?",
         (a, b, now, c["match_id"]))
    return _combat(c["match_id"])


def _qte_start(c: dict, now: float):
    n = c["qte_nonce"] + 1
    # Both players get a separate, simultaneous code. Never depend on frontend RNG.
    def combo():
        return ''.join(RNG.choice(string.ascii_uppercase) for _ in range(RNG.randint(1, 3)))
    da, db_code = combo(), combo()
    until = min(c["phase_until"] - .5, now + QTE_SECONDS)
    db.x("""UPDATE pvp_combat SET qte_nonce=?,qte_a=?,qte_b=?,qte_done_a=0,qte_done_b=0,
       qte_until=?,next_qte=?,qte_count=qte_count+1 WHERE match_id=?""",
         (n, da, db_code, until, now + RNG.uniform(6.0, 7.5), c["match_id"]))
    return _event(_combat(c["match_id"]), "qte", now, nonce=n, until=until)


def _combat_tick(m: dict, now: float):
    """Advance combat in small intervals. Persist every applied hit; restarts resume.

    Only one process/worker is supported, but db.tx serializes HTTP and ticker.
    """
    c = _combat(m["id"])
    if not c:
        raise RuntimeError("Missing combat state")
    stop = min(now, c["phase_until"])
    # Deliberately do NOT damage either pet here. A clock tick only schedules
    # QTE prompts and the next betting phase; no unattended/passive attacks.
    # Issue up to two surprise QTEs each combat phase.
    if (c["qte_count"] < 2 and stop >= c["next_qte"] and
            (not c["qte_until"] or stop >= c["qte_until"]) and
            stop + 2.0 < c["phase_until"]):
        _qte_start(c, stop)
    if now >= c["phase_until"]:
        c = _combat(m["id"])
        rounds = json.loads(m["rounds_json"])
        rounds.append({"round": m["round_no"], "hp_a": c["hp_a"], "hp_b": c["hp_b"]})
        next_round = m["round_no"] + 1
        starter = m["defender_id"] if next_round % 2 == 0 else m["challenger_id"]
        db.x("""UPDATE pvp_matches SET status='betting',rounds_json=?,round_no=?,
          street_a=0,street_b=0,checks=0,raises=0,turn_user=?,deadline=? WHERE id=?""",
             (json.dumps(rounds), next_round, starter, now + TURN_SECONDS, m["id"]))
        _event(c, "pause", now, round=m["round_no"])
    return False


def _process_timeout(m: dict, now: float):
    if m["status"] == "invited":
        _cancel(m, "expired", now)
    elif m["status"] == "reveal":
        _combat_tick(m, now)
    elif m["status"] == "betting":
        uid = m["turn_user"]
        a, b = (m["street_a"], m["street_b"]) if uid == m["challenger_id"] else (m["street_b"], m["street_a"])
        if a < b:
            _finish(m, _other(m, uid), "timeout_fold", now)
        else:
            checks = m["checks"] + 1
            if checks >= 2:
                _round(m, now)
            else:
                db.x("UPDATE pvp_matches SET checks=?,turn_user=?,deadline=? WHERE id=?",
                     (checks, _other(m, uid), now + TURN_SECONDS, m["id"]))


def combat_step():
    affected = set()
    now = time.time()
    with db.tx():
        for m in db.q("SELECT * FROM pvp_matches WHERE status='reveal' ORDER BY id LIMIT 100"):
            before = _combat(m["id"])
            if before:
                _combat_tick(m, now)
                after = _combat(m["id"])
                if after and (after["event_seq"] != before["event_seq"] or
                              _row(m["id"])["status"] != 'reveal'):
                    affected.update((m["challenger_id"], m["defender_id"]))
    if affected:
        notify(*affected)

def expire_due():
    affected = set()
    now = time.time()
    with db.tx():
        due = db.q("""SELECT * FROM pvp_matches WHERE status IN ('invited','betting')
                       AND deadline<=? ORDER BY id LIMIT 100""", (now,))
        for m in due:
            _process_timeout(m, now)
            affected.update((m["challenger_id"], m["defender_id"]))
    if affected:
        notify(*affected)
    return len(due)


async def expiry_loop():
    while True:
        try:
            await run_in_threadpool(expire_due)
            await run_in_threadpool(combat_step)
        except Exception as ex:
            print("[pvp expiry]", repr(ex))
        await asyncio.sleep(.5)


# -------------------------- transitions -------------------------------------
class _Replay(Exception):
    """Internal marker: transaction is rolled back before returning fresh state."""


def idempotent(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(uid, *args):
        try:
            return fn(uid, *args)
        except _Replay:
            return get_state(uid)
    return wrapper



@idempotent
def invite(uid: int, defender: int, bet: int, rid: str):
    if uid == defender:
        raise HTTPException(400, "Нельзя вызвать самого себя")
    if bet % STEP:
        raise HTTPException(400, "Ставка должна быть кратна 10")
    expire_due()
    now = time.time()
    invite_action = f"invite:{defender}:{bet}"
    with db.tx():
        old = _request(uid, rid, 0, invite_action)
        if old:
            raise _Replay()
        if _active_match(uid) or _active_match(defender):
            raise HTTPException(409, "Один из игроков уже участвует в матче")
        other = social.require_friend(uid, defender)
        me = db.one("SELECT username,display_name FROM users WHERE id=?", (uid,))
        _eligible(uid, now)
        _eligible(defender, now)
        _limits(uid, defender, now)
        _limits(defender, uid, now)
        mid = db.x("""INSERT INTO pvp_matches(challenger_id,defender_id,challenger_name,defender_name,status,
          initial_bet,pot,total_a,total_b,deadline,created_at) VALUES(?,?,?,?,'invited',?,?,?,0,?,?)""",
          (uid, defender, me["display_name"] or me["username"], other["display_name"] or other["username"],
           bet, bet, bet, now + INVITE_SECONDS, now))
        _debit(mid, uid, bet, "invite", now)
        _record_request(uid, rid, mid, invite_action)
    notify(uid, defender)
    return get_state(uid)


@idempotent
def respond(uid: int, mid: int, rid: str, move: str):
    assert move in ("accept", "decline", "cancel")
    expire_due()
    now = time.time()
    with db.tx():
        old = _request(uid, rid, mid, move)
        if old:
            raise _Replay()
        m = _row(mid)
        if not m or not _mine(m, uid):
            raise HTTPException(404, "Приглашение не найдено")
        if m["status"] != "invited":
            raise HTTPException(409, "Приглашение уже обработано")
        if move == "cancel" and uid != m["challenger_id"]:
            raise HTTPException(403, "Отменить вызов может только инициатор")
        if move in ("accept", "decline") and uid != m["defender_id"]:
            raise HTTPException(403, "Ответить должен приглашённый игрок")
        if move != "accept":
            _cancel(m, move, now)
        else:
            if not is_online(m["challenger_id"]) or not is_online(m["defender_id"]):
                raise HTTPException(409, "Для принятия боя оба игрока должны находиться онлайн")
            a, b = _eligible(m["challenger_id"], now), _eligible(uid, now)
            _debit(mid, uid, m["initial_bet"], "accept", now)
            db.x("""UPDATE pvp_matches SET status='betting',pot=pot+?,total_b=total_b+?,
              power_a=?,power_b=?,pet_a=?,pet_b=?,turn_user=?,deadline=? WHERE id=?""",
              (m["initial_bet"], m["initial_bet"], _power(a, now), _power(b, now),
               json.dumps(pet._view(a, now), ensure_ascii=False), json.dumps(pet._view(b, now), ensure_ascii=False),
               m["challenger_id"], now + TURN_SECONDS, mid))
            db.x("INSERT OR IGNORE INTO pvp_combat(match_id,energy_at,tick_at) VALUES(?,?,?)", (mid, now, now))
        _record_request(uid, rid, mid, move)
    notify(m["challenger_id"], m["defender_id"])
    return get_state(uid)


@idempotent
def action(uid: int, mid: int, rid: str, move: str, amount: int):
    expire_due()
    now = time.time()
    with db.tx():
        old = _request(uid, rid, mid, move + ":" + str(amount))
        if old:
            raise _Replay()
        m = _row(mid)
        if not m or not _mine(m, uid):
            raise HTTPException(404, "Матч не найден")
        if m["status"] != "betting" or uid != m["turn_user"]:
            raise HTTPException(409, "Сейчас не твой ход")
        own_a = uid == m["challenger_id"]
        my_key, their_key = ("street_a", "street_b") if own_a else ("street_b", "street_a")
        my_total = m["total_a"] if own_a else m["total_b"]
        to_call = max(0, m[their_key] - m[my_key])
        if move == "fold":
            if amount:
                raise HTTPException(400, "При сдаче нельзя указывать ставку")
            _finish(m, _other(m, uid), "fold", now)
        elif move == "check":
            if amount or to_call:
                raise HTTPException(400, "Нельзя пропустить ход, пока ставка не уравнена")
            if m["checks"]:
                _round(m, now)
            else:
                db.x("UPDATE pvp_matches SET checks=1,turn_user=?,deadline=? WHERE id=?",
                     (_other(m, uid), now + TURN_SECONDS, mid))
        elif move == "call":
            if amount or to_call <= 0:
                raise HTTPException(400, "Нет ставки для уравнивания")
            if my_total + to_call > MAX_COMMITTED:
                raise HTTPException(400, "Превышен лимит ставки на игрока")
            _debit(mid, uid, to_call, "call", now)
            db.x(f"UPDATE pvp_matches SET {my_key}={my_key}+?," +
                 ("total_a=total_a+?," if own_a else "total_b=total_b+?,") +
                 "pot=pot+? WHERE id=?", (to_call, to_call, to_call, mid))
            _round(_row(mid), now)
        elif move == "raise":
            if amount < MIN_BET or amount > MAX_BET or amount % STEP:
                raise HTTPException(400, "Повышение: от 10 до 200, шаг 10")
            if m["raises"] >= MAX_RAISES:
                raise HTTPException(400, "В этом раунде больше повышать нельзя")
            debit = to_call + amount
            if my_total + debit > MAX_COMMITTED:
                raise HTTPException(400, "Превышен лимит ставки на игрока")
            # Opponent must be able to cover the raise, including already committed chips.
            other_total = m["total_b"] if own_a else m["total_a"]
            available = slots.ensure_account(_other(m, uid))["balance"]
            if other_total + available < my_total + debit:
                raise HTTPException(400, "Противнику не хватит жетонов для уравнивания")
            _debit(mid, uid, debit, "raise", now)
            db.x(f"UPDATE pvp_matches SET {my_key}={my_key}+?," +
                 ("total_a=total_a+?," if own_a else "total_b=total_b+?,") +
                 "pot=pot+?,checks=0,raises=raises+1,turn_user=?,deadline=? WHERE id=?",
                 (debit, debit, debit, _other(m, uid), now + TURN_SECONDS, mid))
        _record_request(uid, rid, mid, move + ":" + str(amount))
    notify(m["challenger_id"], m["defender_id"])
    return get_state(uid)


@idempotent
def combat_action(uid: int, mid: int, rid: str, move: str):
    expire_due()
    now = time.time()
    with db.tx():
        if _request(uid, rid, mid, "combat:" + move):
            raise _Replay()
        m = _row(mid)
        if not m or not _mine(m, uid):
            raise HTTPException(404, "Бой не найден")
        if m["status"] != "reveal" or m["deadline"] <= now:
            raise HTTPException(409, "Сейчас нельзя наносить удары")
        c = _regen(_combat(mid), now)
        own = uid == m["challenger_id"]
        energy_key = "energy_a" if own else "energy_b"
        last_key = "attack_a" if own else "attack_b"
        guard_key = "guard_a" if own else "guard_b"
        if move == "attack":
            if now - c[last_key] < ATTACK_INTERVAL or c[energy_key] < 12:
                raise HTTPException(429, "Атака восстанавливается или не хватает энергии")
            db.x(f"UPDATE pvp_combat SET {energy_key}={energy_key}-12,{last_key}=? WHERE match_id=?", (now, mid))
            _damage(m, uid, "attack", now)
        elif move == "guard":
            if now - c[guard_key] < GUARD_INTERVAL or c[energy_key] < 8:
                raise HTTPException(429, "Защита восстанавливается или не хватает энергии")
            db.x(f"UPDATE pvp_combat SET {energy_key}={energy_key}-8,{guard_key}=? WHERE match_id=?", (now + 1.1, mid))
            _event(_combat(mid), "guard", now, source=uid)
        _record_request(uid, rid, mid, "combat:" + move)
    notify(m["challenger_id"], m["defender_id"])
    return get_state(uid)


@idempotent
def qte_action(uid: int, mid: int, rid: str, nonce: int, sequence: str):
    expire_due()
    now = time.time()
    with db.tx():
        key = f"qte:{nonce}:{sequence}"
        if _request(uid, rid, mid, key):
            raise _Replay()
        m = _row(mid)
        if not m or not _mine(m, uid):
            raise HTTPException(404, "Бой не найден")
        c = _combat(mid)
        if m["status"] != 'reveal' or not c or c["qte_nonce"] != nonce or now > c["qte_until"]:
            raise HTTPException(409, "Комбинация уже недоступна")
        mine = uid == m["challenger_id"]
        code = c["qte_a"] if mine else c["qte_b"]
        done_key = "qte_done_a" if mine else "qte_done_b"
        if c[done_key]:
            raise HTTPException(409, "Комбинация уже выполнена")
        if sequence != code:
            raise HTTPException(400, "Неверная комбинация")
        db.x(f"UPDATE pvp_combat SET {done_key}=1 WHERE match_id=?", (mid,))
        _event(_combat(mid), "critical", now, source=uid, combo=code)
        _damage(m, uid, "critical", now)
        _record_request(uid, rid, mid, key)
    notify(m["challenger_id"], m["defender_id"])
    return get_state(uid)


# -------------------------- read model --------------------------------------
def _combat_view(m: dict, uid: int):
    c = _combat(m["id"])
    if not c:
        return None
    own = uid == m["challenger_id"]
    return {"hp_me": c["hp_a"] if own else c["hp_b"], "hp_other": c["hp_b"] if own else c["hp_a"],
            "hp_max": BASE_HP, "energy": round(c["energy_a"] if own else c["energy_b"], 1),
            "events": json.loads(c["events_json"]), "event_seq": c["event_seq"],
            "qte_nonce": c["qte_nonce"], "qte_code": (c["qte_a"] if own else c["qte_b"]) if m["status"] == "reveal" and c["qte_until"] > time.time() else "",
            "qte_done": bool(c["qte_done_a"] if own else c["qte_done_b"]),
            "qte_until": c["qte_until"], "phase_until": c["phase_until"]}


def _present(m: dict, uid: int):
    own_a = uid == m["challenger_id"]
    mine = m["challenger_id"] if own_a else m["defender_id"]
    enemy = _other(m, mine)
    mine_street = m["street_a"] if own_a else m["street_b"]
    their_street = m["street_b"] if own_a else m["street_a"]
    to_call = max(0, their_street - mine_street)
    enemy_balance = slots.ensure_account(enemy)["balance"] if m["status"] == "betting" else 0
    my_total = m["total_a"] if own_a else m["total_b"]
    their_total = m["total_b"] if own_a else m["total_a"]
    my_acc = slots.ensure_account(mine)["balance"]
    raise_max = min(MAX_BET, MAX_COMMITTED - my_total - to_call,
                    max(0, their_total + enemy_balance - my_total - to_call),
                    max(0, my_acc - to_call))
    raise_max = max(0, raise_max // STEP * STEP)
    return {
        "id": m["id"], "status": m["status"], "initiator": m["challenger_id"],
        "me": mine, "other": enemy,
        "me_name": m["challenger_name"] if own_a else m["defender_name"],
        "other_name": m["defender_name"] if own_a else m["challenger_name"],
        "pet_me": json.loads(m["pet_a"] if own_a else m["pet_b"]) if m["pet_a"] else None,
        "pet_other": json.loads(m["pet_b"] if own_a else m["pet_a"]) if m["pet_b"] else None,
        "power_me": m["power_a"] if own_a else m["power_b"],
        "power_other": m["power_b"] if own_a else m["power_a"],
        "pot": m["pot"], "initial_bet": m["initial_bet"],
        "my_total": my_total, "other_total": their_total,
        "my_round_bet": mine_street, "other_round_bet": their_street,
        "round_no": m["round_no"],
        "wins_me": m["wins_a"] if own_a else m["wins_b"],
        "wins_other": m["wins_b"] if own_a else m["wins_a"],
        "rounds": json.loads(m["rounds_json"]),
        "combat": _combat_view(m, uid),
        "turn_user": m["turn_user"], "my_turn": m["turn_user"] == uid and m["status"] == "betting",
        "to_call": to_call, "raises_left": MAX_RAISES - m["raises"], "raise_max": raise_max,
        "winner_id": m["winner_id"], "reason": m["result_reason"], "deadline": m["deadline"],
        "other_online": is_online(enemy), "created_at": m["created_at"], "ended_at": m["ended_at"],
    }


def get_state(uid: int):
    expire_due()
    now = time.time()
    with db.tx():
        balance = slots.ensure_account(uid)["balance"]
        active = _active_match(uid)
        latest = db.q("""SELECT * FROM pvp_matches WHERE status IN ('finished','cancelled')
                     AND (challenger_id=? OR defender_id=?) ORDER BY id DESC LIMIT 6""", (uid, uid))
        # Don't expose other players' balance; only the minimal betting state.
        result = {"active": _present(active, uid) if active else None,
                  "recent": [_present(m, uid) for m in latest], "balance": balance, "server_now": now,
                  "limits": {"min_bet": MIN_BET, "max_bet": MAX_BET, "step": STEP, "daily": DAILY_LIMIT,
                             "pair": PAIR_LIMIT, "turn_seconds": TURN_SECONDS, "invite_seconds": INVITE_SECONDS,
                             "combat_seconds": COMBAT_SECONDS},
                  "pet": None, "friends": []}
        own = pet._get(uid)
        result["pet"] = pet._view(own, now) if own else None
        rows = db.q("""SELECT u.id,u.username,u.display_name,p.species,p.name,p.xp,p.fullness,p.fed_at
             FROM friendships f JOIN users u ON u.id=CASE WHEN f.requester_id=? THEN f.addressee_id ELSE f.requester_id END
             LEFT JOIN pets p ON p.user_id=u.id
             WHERE f.status='accepted' AND (f.requester_id=? OR f.addressee_id=?)
               AND COALESCE(u.banned,0)=0 AND u.username<>?
             ORDER BY lower(COALESCE(NULLIF(u.display_name,''),u.username))""",
             (uid, uid, uid, auth.SYSTEM_USERNAME))
        for r in rows:
            rp = {"species": r["species"], "xp": r["xp"], "fullness": r["fullness"], "fed_at": r["fed_at"]} if r["species"] else None
            result["friends"].append({"id": r["id"], "name": r["display_name"] or r["username"],
                "pet": {"species": r["species"], "name": r["name"], "power": _power(rp, now),
                        "fullness": round(pet._fullness(rp, now))} if rp else None,
                "online": is_online(r["id"]), "busy": bool(_active_match(r["id"]))})
    return result


def delete_user_data(uid: int):
    """Settle outstanding escrow before slot account is deleted."""
    affected = set()
    with db.tx():
        active = db.q("SELECT * FROM pvp_matches WHERE (challenger_id=? OR defender_id=?) AND status IN ('invited','betting','reveal')", (uid, uid))
        for m in active:
            if m["status"] == "invited":
                _cancel(m, "account_deleted", time.time())
            else:
                _finish(m, _other(m, uid), "account_deleted", time.time())
            affected.add(_other(m, uid))
    if affected:
        notify(*affected)


# -------------------------- HTTP --------------------------------------------
def _me(request: Request):
    return auth.require_user(request)["id"]


@router.get("")
async def api_state(request: Request):
    return await run_in_threadpool(get_state, _me(request))


@router.post("/invite")
async def api_invite(request: Request, body: InviteIn):
    return await run_in_threadpool(invite, _me(request), body.defender_id, body.bet, body.request_id)


@router.post("/accept")
async def api_accept(request: Request, body: MatchIn):
    return await run_in_threadpool(respond, _me(request), body.match_id, body.request_id, "accept")


@router.post("/decline")
async def api_decline(request: Request, body: MatchIn):
    return await run_in_threadpool(respond, _me(request), body.match_id, body.request_id, "decline")


@router.post("/cancel")
async def api_cancel(request: Request, body: MatchIn):
    return await run_in_threadpool(respond, _me(request), body.match_id, body.request_id, "cancel")


@router.post("/action")
async def api_action(request: Request, body: ActionIn):
    return await run_in_threadpool(action, _me(request), body.match_id, body.request_id, body.move, body.amount)


@router.post("/combat")
async def api_combat(request: Request, body: CombatIn):
    return await run_in_threadpool(combat_action, _me(request), body.match_id, body.request_id, body.move)


@router.post("/qte")
async def api_qte(request: Request, body: QteIn):
    return await run_in_threadpool(qte_action, _me(request), body.match_id, body.request_id, body.nonce, body.sequence)
