"""Общее для локаций фермы (пасека, шахта, караван): улучшения, замок по уровню фермы, оплата монетами.

Каждая локация хранит свои улучшения в таблице `<имя>_upgrades(user_id, key, level)`; монеты — монеты фермы (farm.py), поэтому все локации — одна экономика.
Всё вызывается внутри db.tx() (кроме чтения).
"""
from fastapi import HTTPException

from . import farm
from .db import db


def levels(table: str, user_id: int, keys) -> dict:
    lv = {k: 0 for k in keys}
    for r in db.q(f"SELECT key, level FROM {table} WHERE user_id=?", (user_id,)):
        if r["key"] in lv:
            lv[r["key"]] = r["level"]
    return lv


def need_open(user_id: int, need_level: int, name: str) -> dict:
    """Фермерская строка игрока; ошибка, если уровень фермы ниже нужного."""
    f = farm._farm(user_id)
    if farm._level(f) < need_level:
        raise HTTPException(400, f"{name} откроется на уровне фермы {need_level}")
    return f


def upgrade_view(ups: dict, lv: dict) -> list:
    return [{"key": k, "name": u["name"], "icon": u["icon"], "desc": u["desc"], "level": lv[k], "max": u["max"], "cost": u["costs"][lv[k]] if lv[k] < u["max"] else None}
            for k, u in ups.items()]


def buy_upgrade(user_id: int, table: str, ups: dict, key: str, f: dict, cur: int):
    """Списать монеты и поднять уровень улучшения (f — строка фермы, cur — текущий уровень)."""
    u = ups.get(key)
    if not u:
        raise HTTPException(400, "Такого улучшения нет")
    if cur >= u["max"]:
        raise HTTPException(400, "Улучшение уже на максимуме")
    farm._pay(user_id, f, u["costs"][cur])
    db.x(f"INSERT INTO {table}(user_id, key, level) VALUES(?,?,1) ON CONFLICT(user_id, key) DO UPDATE SET level=level+1", (user_id, key))


def make_upgrades(rows: list) -> dict:
    """(ключ, название, значок, описание, цены по уровням) → словарь улучшений."""
    return {u[0]: {"key": u[0], "name": u[1], "icon": u[2], "desc": u[3], "costs": u[4], "max": len(u[4])} for u in rows}
