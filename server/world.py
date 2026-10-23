"""Карта мира фермы: список локаций, замки по уровню фермы и значки «что-то готово» (одна страница `#/farm/map`).

Ферма — центр, вокруг неё локации: лаборатория (подвал), озеро, пасека, шахта и торговый тракт. Данные берутся у самих локаций, чтобы правила замков не дублировались.
"""
import time

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from . import apiary, auth, caravan, farm, lab, lake, mine
from .db import db

router = APIRouter(prefix="/api/world", tags=["Карта мира"])

# id, название, значок, маршрут на сайте, нужный уровень фермы (берём у модулей локаций), что готово
LOCATIONS = [
    ("farm", "Ферма", "🚜", "#/farm", 1, lambda uid: farm_ready(uid)),
    ("lab", "Лаборатория", "🔬", "#/farm/lab", lab.UNLOCK_FARM_LEVEL, lambda uid: lab_ready(uid)),
    ("lake", "Озеро", "🎣", "#/farm/lake", lake.UNLOCK_FARM_LEVEL, lambda uid: lake_ready(uid)),
    ("apiary", "Пасека", "🐝", "#/farm/apiary", apiary.UNLOCK_FARM_LEVEL, apiary.ready_count),
    ("mine", "Шахта", "⛏️", "#/farm/mine", mine.UNLOCK_FARM_LEVEL, mine.ready_count),
    ("caravan", "Торговый тракт", "🐎", "#/farm/caravan", caravan.UNLOCK_FARM_LEVEL, caravan.ready_count),
]


def farm_ready(uid: int) -> int:
    now = time.time()
    return (db.val("SELECT COUNT(*) FROM farm_plots WHERE user_id=? AND ready_at<=?", (uid, now), 0) or 0) + \
        (db.val("SELECT COUNT(*) FROM farm_pens WHERE user_id=? AND fed_count>0 AND ready_at<=?", (uid, now), 0) or 0)


def lake_ready(uid: int) -> int:
    return db.val("SELECT COUNT(*) FROM lake_rods WHERE user_id=? AND ready_at<=?", (uid, time.time()), 0) or 0


def lab_ready(uid: int) -> int:
    try:
        return sum(1 for t in lab.get_state(uid)["trees"] if t.get("state") == "mature" and t.get("cones", 0) > 0)
    except Exception:
        return 0


def get_world(user_id: int) -> dict:
    lvl = farm._level(farm._farm(user_id))
    out = []
    for lid, name, icon, route, need, ready in LOCATIONS:
        ok = lvl >= need
        out.append({"id": lid, "name": name, "icon": icon, "route": route, "need_level": need, "unlocked": ok, "ready": ready(user_id) if ok else 0})
    return {"farm_level": lvl, "locations": out}


@router.get("")
async def api_world(request: Request):
    return await run_in_threadpool(get_world, auth.require_user(request)["id"])
