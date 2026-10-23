"""Данные «Каравана» (торговый тракт фермы): города, спрос, улучшения. Всё придумано для LearnQuest.

Караван везёт товары со склада фермы в дальние города: чем дальше город, тем выше цена, но тем дольше дорога и тем опаснее разбойники. Каждый день в каждом городе
два «ходовых» товара, за них платят в полтора раза больше. Цена и разбойники определяются при отправке и хранятся в записи рейса (повторными нажатиями не перебросить).
Для проверки время можно ускорить переменной окружения CARAVAN_SPEED (например, 120 — час превращается в полминуты).
"""
import hashlib
import os

from .farm_data import ITEMS
from .loc_common import make_upgrades

try:
    SPEED = max(1.0, float(os.environ.get("CARAVAN_SPEED", "1")))
except ValueError:
    SPEED = 1.0

UNLOCK_FARM_LEVEL = 13
START_WAGONS = 1
BASE_CARGO = 30               # штук товара в одной повозке без улучшений
CARGO_STEP = 10
WANT_MULT = 1.5               # ходовой товар города дороже в полтора раза
BANDIT_LOSS = 0.4             # доля выручки, которую уносят разбойники
HORSE_STEP = 0.07             # −7 % ко времени дороги за уровень лошадей
GUARD_STEP = 0.02             # −2 % к шансу нападения за уровень охраны
M = 60

# город: id, название, значок, дорога (сек), множитель цены, шанс нападения, нужный уровень дорог
_TOWNS = [
    ("village", "Соседний посёлок", "🏘️", 20 * M, 1.25, 0.00, 0),
    ("harbor", "Речной порт", "⚓", 45 * M, 1.50, 0.03, 0),
    ("fort", "Горный форт", "🏰", 90 * M, 1.80, 0.08, 1),
    ("capital", "Столица", "🏛️", 180 * M, 2.40, 0.15, 2),
]
TOWNS = {t[0]: {"id": t[0], "name": t[1], "icon": t[2], "time": int(t[3] / SPEED), "mult": t[4], "risk": t[5], "roads": t[6]} for t in _TOWNS}

UPGRADES = make_upgrades([
    ("wagons", "Повозки", "🛒", "+1 повозка (старт 1, максимум 4)", [1200, 4000, 10000]),
    ("cart", "Грузоподъёмность", "📦", "+10 штук товара в повозке за уровень (старт 30)", [400, 900, 2000, 4400, 9500]),
    ("horse", "Лошади", "🐎", "−7 % ко времени дороги за уровень", [600, 1500, 3200, 7000, 15000]),
    ("guard", "Охрана", "🛡️", "−2 % к шансу нападения разбойников за уровень", [800, 2000, 4500, 10000, 22000]),
    ("roads", "Дороги", "🛣️", "уровень 1 открывает Горный форт, уровень 2 — Столицу", [3500, 14000]),
    ("merchant", "Купец", "🧮", "+4 % к выручке за уровень", [1000, 2500, 6000, 13000, 28000]),
])


def wagons_count(level: int) -> int:
    return START_WAGONS + level


def cargo_cap(level: int) -> int:
    return BASE_CARGO + CARGO_STEP * level


def trip_time(town: str, horse: int) -> int:
    return max(1, int(TOWNS[town]["time"] * (1 - HORSE_STEP * horse)))


def bandit_chance(town: str, guard: int) -> float:
    return max(0.0, TOWNS[town]["risk"] - GUARD_STEP * guard)


def town_open(town: str, roads: int) -> bool:
    return TOWNS[town]["roads"] <= roads


def wanted(town: str, day: str) -> list[str]:
    """Два ходовых товара города на сегодня (одинаково у всех игроков)."""
    ids = sorted(ITEMS)
    out: list[str] = []
    n = 0
    while len(out) < 2:
        h = int(hashlib.sha256(f"{day}:{town}:{n}".encode()).hexdigest()[:8], 16)
        it = ids[h % len(ids)]
        if it not in out:
            out.append(it)
        n += 1
    return out


def unit_price(item: str, town: str, day: str, merchant: int) -> int:
    k = TOWNS[town]["mult"] * (WANT_MULT if item in wanted(town, day) else 1) * (1 + .04 * merchant)
    return max(1, int(ITEMS[item]["price"] * k))
