"""Друзья, подписи на страничке друга (с модерацией администратором) и управление пользователями (просмотр, бан).

Друзья — взаимные: одна сторона отправляет заявку по логину, вторая принимает. Прогресс друга и подписи видны только друзьям.
Подпись: до 40 символов, одна от каждого автора на страничку; пока админ не подтвердит, на страничке её видит только автор («на модерации»).
"""
import re

from fastapi import HTTPException

from . import auth
from . import gamification as gm
from .db import db

SIGN_MAX = 40
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


# ------------------------------------------------------------------ пользователи
def _visible(u: dict | None) -> bool:
    return bool(u) and u["username"] != auth.SYSTEM_USERNAME and not u.get("banned")


def find_user(username: str) -> dict | None:
    u = db.one("SELECT * FROM users WHERE username=?", (username.strip().lstrip("@"),))
    return u if _visible(u) else None


def last_active(uid: int) -> str | None:
    return db.val("SELECT MAX(day) FROM activity WHERE user_id=? AND (lessons>0 OR xp>0)", (uid,))


def brief(u: dict) -> dict:
    xp = gm.total_xp(u["id"])
    return {"id": u["id"], "username": u["username"], "display_name": u["display_name"] or u["username"], "avatar": u["avatar"],
            "level": gm.level_info(xp)["level"], "xp": xp, "streak": gm.streak_info(u["id"])["current"], "last_active": last_active(u["id"])}


# ------------------------------------------------------------------ друзья
def _pair(a: int, b: int) -> dict | None:
    return db.one("SELECT * FROM friendships WHERE (requester_id=? AND addressee_id=?) OR (requester_id=? AND addressee_id=?)", (a, b, b, a))


def are_friends(a: int, b: int) -> bool:
    r = _pair(a, b)
    return bool(r and r["status"] == "accepted")


def require_friend(me_id: int, other_id: int) -> dict:
    u = db.one("SELECT * FROM users WHERE id=?", (other_id,))
    if not _visible(u) or not are_friends(me_id, other_id):
        raise HTTPException(404, "Друг не найден")
    return u


def overview(me_id: int) -> dict:
    friends, incoming, outgoing = [], [], []
    for r in db.q("SELECT * FROM friendships WHERE requester_id=? OR addressee_id=? ORDER BY created_at DESC", (me_id, me_id)):
        other_id = r["addressee_id"] if r["requester_id"] == me_id else r["requester_id"]
        u = db.one("SELECT * FROM users WHERE id=?", (other_id,))
        if not _visible(u):
            continue
        if r["status"] == "accepted":
            friends.append(brief(u))
        elif r["requester_id"] == me_id:
            outgoing.append({"id": u["id"], "username": u["username"], "display_name": u["display_name"] or u["username"], "avatar": u["avatar"]})
        else:
            incoming.append({"id": u["id"], "username": u["username"], "display_name": u["display_name"] or u["username"], "avatar": u["avatar"]})
    friends.sort(key=lambda f: (-(f["streak"] or 0), -f["xp"]))
    return {"friends": friends, "incoming": incoming, "outgoing": outgoing}


def request_friend(me_id: int, username: str) -> dict:
    other = find_user(username)
    if not other:
        raise HTTPException(404, "Пользователь с таким логином не найден")
    if other["id"] == me_id:
        raise HTTPException(400, "Это ваш собственный логин")
    r = _pair(me_id, other["id"])
    if r:
        if r["status"] == "accepted":
            raise HTTPException(400, "Вы уже друзья")
        if r["requester_id"] == me_id:
            raise HTTPException(400, "Заявка уже отправлена — ждём ответа")
        db.x("UPDATE friendships SET status='accepted' WHERE requester_id=? AND addressee_id=?", (other["id"], me_id))  # встречная заявка = согласие
        return {"status": "accepted", "user": brief(other)}
    db.x("INSERT INTO friendships(requester_id, addressee_id, status) VALUES(?,?, 'pending')", (me_id, other["id"]))
    return {"status": "pending", "user": {"id": other["id"], "username": other["username"], "display_name": other["display_name"] or other["username"], "avatar": other["avatar"]}}


def accept(me_id: int, other_id: int):
    n = db.x("UPDATE friendships SET status='accepted' WHERE requester_id=? AND addressee_id=? AND status='pending'", (other_id, me_id))
    if not db.one("SELECT 1 AS x FROM friendships WHERE requester_id=? AND addressee_id=? AND status='accepted'", (other_id, me_id)):
        raise HTTPException(404, "Заявка не найдена")
    return n


def decline_or_cancel(me_id: int, other_id: int):
    """Отклонить входящую или отозвать исходящую заявку."""
    db.x("DELETE FROM friendships WHERE status='pending' AND ((requester_id=? AND addressee_id=?) OR (requester_id=? AND addressee_id=?))",
         (me_id, other_id, other_id, me_id))


def remove_friend(me_id: int, other_id: int):
    """Удалить из друзей; подписи друг на друге при этом исчезают."""
    db.x("DELETE FROM friendships WHERE (requester_id=? AND addressee_id=?) OR (requester_id=? AND addressee_id=?)", (me_id, other_id, other_id, me_id))
    db.x("DELETE FROM signatures WHERE (target_id=? AND author_id=?) OR (target_id=? AND author_id=?)", (me_id, other_id, other_id, me_id))


# ------------------------------------------------------------------ подписи
def clean_signature(text: str) -> str:
    t = _CTRL.sub(" ", text or "")
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        raise HTTPException(400, "Подпись не может быть пустой")
    if len(t) > SIGN_MAX:
        raise HTTPException(400, f"Подпись длиннее {SIGN_MAX} символов")
    return t


def _sig_out(r: dict, author: dict | None = None) -> dict:
    out = {"id": r["id"], "text": r["text"], "status": r["status"], "created_at": r["created_at"], "updated_at": r["updated_at"]}
    if author:
        out["author"] = {"id": author["id"], "username": author["username"], "display_name": author["display_name"] or author["username"], "avatar": author["avatar"]}
    return out


def wall(target_id: int) -> list[dict]:
    """Подтверждённые подписи на страничке пользователя (новые сверху)."""
    out = []
    for r in db.q("SELECT * FROM signatures WHERE target_id=? AND status='approved' ORDER BY updated_at DESC, id DESC", (target_id,)):
        a = db.one("SELECT * FROM users WHERE id=?", (r["author_id"],))
        if _visible(a):
            out.append(_sig_out(r, a))
    return out


def my_signature(author_id: int, target_id: int) -> dict | None:
    r = db.one("SELECT * FROM signatures WHERE author_id=? AND target_id=?", (author_id, target_id))
    return _sig_out(r) if r else None


def leave_signature(author: dict, target_id: int, text: str) -> dict:
    require_friend(author["id"], target_id)
    t = clean_signature(text)
    status = "approved" if author["is_admin"] else "pending"  # подписи самого администратора модерировать некому
    old = db.one("SELECT * FROM signatures WHERE author_id=? AND target_id=?", (author["id"], target_id))
    if old and old["text"] == t and old["status"] in ("pending", "approved"):
        return _sig_out(old)
    if old:
        db.x("UPDATE signatures SET text=?, status=?, updated_at=CURRENT_TIMESTAMP, moderated_by=NULL WHERE id=?", (t, status, old["id"]))
        sid = old["id"]
    else:
        sid = db.x("INSERT INTO signatures(target_id, author_id, text, status) VALUES(?,?,?,?)", (target_id, author["id"], t, status))
    return _sig_out(db.one("SELECT * FROM signatures WHERE id=?", (sid,)))


def delete_my_signature(author_id: int, target_id: int):
    db.x("DELETE FROM signatures WHERE author_id=? AND target_id=?", (author_id, target_id))


def delete_from_my_wall(me_id: int, sig_id: int):
    """Владелец странички может убрать любую подпись у себя."""
    if not db.one("SELECT 1 AS x FROM signatures WHERE id=? AND target_id=?", (sig_id, me_id)):
        raise HTTPException(404, "Подпись не найдена")
    db.x("DELETE FROM signatures WHERE id=?", (sig_id,))


# ------------------------------------------------------------------ модерация (админ)
def pending_count() -> int:
    return db.val("""SELECT COUNT(*) FROM signatures s JOIN users a ON a.id=s.author_id JOIN users t ON t.id=s.target_id
                     WHERE s.status='pending' AND COALESCE(a.banned,0)=0 AND COALESCE(t.banned,0)=0""", default=0)


def pending_list() -> list[dict]:
    out = []
    for r in db.q("SELECT * FROM signatures WHERE status='pending' ORDER BY created_at, id LIMIT 200"):
        a, t = db.one("SELECT * FROM users WHERE id=?", (r["author_id"],)), db.one("SELECT * FROM users WHERE id=?", (r["target_id"],))
        if not (_visible(a) and _visible(t)):
            continue
        o = _sig_out(r, a)
        o["target"] = {"id": t["id"], "username": t["username"], "display_name": t["display_name"] or t["username"], "avatar": t["avatar"]}
        out.append(o)
    return out


def moderate(sig_id: int, admin_id: int, approve: bool) -> dict:
    r = db.one("SELECT * FROM signatures WHERE id=?", (sig_id,))
    if not r:
        raise HTTPException(404, "Подпись не найдена")
    db.x("UPDATE signatures SET status=?, moderated_by=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", ("approved" if approve else "rejected", admin_id, sig_id))
    return _sig_out(db.one("SELECT * FROM signatures WHERE id=?", (sig_id,)))


# ------------------------------------------------------------------ пользователи (админ)
def users_list(q: str = "") -> list[dict]:
    q = (q or "").strip()
    rows = db.q("SELECT * FROM users WHERE username<>? AND (username LIKE ? OR display_name LIKE ?) ORDER BY id DESC LIMIT 300",
                (auth.SYSTEM_USERNAME, f"%{q}%", f"%{q}%"))
    out = []
    for u in rows:
        xp = gm.total_xp(u["id"])
        out.append({"id": u["id"], "username": u["username"], "display_name": u["display_name"] or u["username"], "avatar": u["avatar"],
                    "created_at": u["created_at"], "is_admin": bool(u["is_admin"]), "banned": bool(u["banned"]), "banned_reason": u["banned_reason"],
                    "banned_at": u["banned_at"], "xp": xp, "level": gm.level_info(xp)["level"],
                    "courses": db.val("SELECT COUNT(*) FROM courses WHERE user_id=? AND is_template=0", (u["id"],), 0), "last_active": last_active(u["id"]),
                    "telegram": bool(u["tg_chat_id"])})
    return out


def ban(admin_id: int, uid: int, reason: str):
    u = db.one("SELECT * FROM users WHERE id=?", (uid,))
    if not u or u["username"] == auth.SYSTEM_USERNAME:
        raise HTTPException(404, "Пользователь не найден")
    if uid == admin_id:
        raise HTTPException(400, "Нельзя заблокировать самого себя")
    if u["is_admin"] or auth.is_admin_name(u["username"]):
        raise HTTPException(400, "Администратора заблокировать нельзя")
    db.x("UPDATE users SET banned=1, banned_reason=?, banned_at=CURRENT_TIMESTAMP WHERE id=?", (reason.strip()[:200], uid))


def unban(uid: int):
    if not db.one("SELECT 1 AS x FROM users WHERE id=? AND username<>?", (uid, auth.SYSTEM_USERNAME)):
        raise HTTPException(404, "Пользователь не найден")
    db.x("UPDATE users SET banned=0, banned_reason=NULL, banned_at=NULL WHERE id=?", (uid,))
