"""Данные фермы (простой аналог «большой фермы»): культуры, животные, рецепты кухни, уровни. Всё придумано для LearnQuest.

Цикл: сажаем культуры на грядках → собираем урожай на склад → кормим животных урожаем → собираем яйца, молоко и т. д. →
готовим блюда на кухне → продаём на рынке (цена меняется каждый день) или сдаём в заказы. За всё идёт опыт, с уровнем открывается новое.
Время идёт само (считается по меткам времени при каждом запросе, фоновых задач нет), поэтому ферма растёт, пока тебя нет.
Деньги фермы — свои «монеты»: их можно обменять на общие жетоны, но с суточным лимитом, чтобы ферма не ломала экономику слотов.
"""
import hashlib

START_COINS = 60
START_PLOTS = 4
MAX_PLOTS = 16
START_CAPACITY = 60
CAPACITY_STEP = 30
MAX_CAPACITY = 300
PEN_SIZE = 4                 # животных в одном загоне
ORDER_SLOTS = 3
EXCHANGE_IN_RATE = 1         # 1 жетон → 1 монета фермы
EXCHANGE_OUT_RATE = 5        # 5 монет фермы → 1 жетон
EXCHANGE_OUT_DAILY = 300     # жетонов в сутки максимум
EXCHANGE_MAX = 5000          # монет за один запрос
MAX_LEVEL = 15

# опыт, нужный для уровня L: 40·(L−1)² (2 → 40, 3 → 160, 5 → 640, 10 → 3240, 15 → 7840)
def xp_for_level(level: int) -> int:
    return 40 * (level - 1) ** 2


def level_of(xp: int) -> int:
    lv = 1
    while lv < MAX_LEVEL and xp >= xp_for_level(lv + 1):
        lv += 1
    return lv


# культура: id → название, значок, цена семян, время роста (сек), сколько собираем, опыт за грядку, уровень
_CROPS = [
    ("wheat", "Пшеница", "🌾", 4, 180, 3, 2, 1),
    ("carrot", "Морковь", "🥕", 6, 300, 3, 3, 1),
    ("corn", "Кукуруза", "🌽", 9, 600, 3, 4, 2),
    ("potato", "Картофель", "🥔", 12, 900, 3, 5, 3),
    ("tomato", "Помидоры", "🍅", 16, 1500, 3, 7, 4),
    ("strawberry", "Клубника", "🍓", 24, 2400, 3, 10, 5),
    ("sunflower", "Подсолнух", "🌻", 32, 3600, 3, 13, 6),
    ("pumpkin", "Тыква", "🎃", 45, 7200, 3, 20, 7),
]
CROPS = {c[0]: {"id": c[0], "name": c[1], "icon": c[2], "seed": c[3], "time": c[4], "yield": c[5], "xp": c[6], "level": c[7]} for c in _CROPS}

# животное: id → название, значок, цена за штуку, чем кормить, время цикла (сек), продукт, опыт за продукт, уровень
_ANIMALS = [
    ("chicken", "Куры", "🐔", 50, "wheat", 300, "egg", 2, 2),
    ("cow", "Коровы", "🐄", 200, "corn", 900, "milk", 5, 4),
    ("pig", "Свиньи", "🐖", 320, "potato", 1800, "bacon", 8, 6),
    ("sheep", "Овцы", "🐑", 480, "carrot", 2700, "wool", 12, 8),
]
ANIMALS = {a[0]: {"id": a[0], "name": a[1], "icon": a[2], "price": a[3], "feed": a[4], "time": a[5], "product": a[6], "xp": a[7], "level": a[8]} for a in _ANIMALS}

# предметы на складе: id → название, значок, базовая цена продажи
_ITEMS = [
    ("wheat", "Пшеница", "🌾", 3), ("carrot", "Морковь", "🥕", 4), ("corn", "Кукуруза", "🌽", 6), ("potato", "Картофель", "🥔", 8),
    ("tomato", "Помидоры", "🍅", 11), ("strawberry", "Клубника", "🍓", 17), ("sunflower", "Подсолнух", "🌻", 22), ("pumpkin", "Тыква", "🎃", 34),
    ("egg", "Яйца", "🥚", 7), ("milk", "Молоко", "🥛", 20), ("bacon", "Бекон", "🥓", 38), ("wool", "Шерсть", "🧶", 60),
    ("bread", "Хлеб", "🍞", 16), ("omelette", "Омлет", "🍳", 28), ("fries", "Картошка фри", "🍟", 36), ("cheese", "Сыр", "🧀", 52),
    ("jam", "Клубничный джем", "🍯", 70), ("pie", "Пирог", "🥧", 110),
]
ITEMS = {i[0]: {"id": i[0], "name": i[1], "icon": i[2], "price": i[3]} for i in _ITEMS}

# рецепт кухни: id → название, что нужно (предмет → сколько), время (сек), что получаем (предмет, штук), опыт, уровень
_RECIPES = [
    ("bread", {"wheat": 3}, 240, 2, 4, 2),
    ("omelette", {"egg": 3}, 360, 1, 6, 3),
    ("fries", {"potato": 3}, 480, 1, 8, 4),
    ("cheese", {"milk": 2}, 720, 1, 12, 5),
    ("jam", {"strawberry": 3}, 900, 1, 16, 6),
    ("pie", {"wheat": 2, "egg": 2, "milk": 1}, 1200, 1, 24, 7),
]
RECIPES = {r[0]: {"id": r[0], "name": ITEMS[r[0]]["name"], "icon": ITEMS[r[0]]["icon"], "needs": r[1], "time": r[2], "qty": r[3], "xp": r[4], "level": r[5]} for r in _RECIPES}


def unlock_level(item_id: str) -> int:
    """С какого уровня предмет вообще можно получить."""
    if item_id in CROPS:
        return CROPS[item_id]["level"]
    for a in ANIMALS.values():
        if a["product"] == item_id:
            return max(a["level"], CROPS[a["feed"]]["level"])
    r = RECIPES.get(item_id)
    return max([r["level"]] + [unlock_level(i) for i in r["needs"]]) if r else 99


def price_mult(item_id: str, day: str) -> float:
    """Цена на рынке «гуляет» от ×0.85 до ×1.30 и меняется раз в сутки (одинакова у всех игроков)."""
    h = int(hashlib.sha256(f"{day}:{item_id}".encode()).hexdigest()[:8], 16)
    return round(0.85 + 0.45 * (h % 1000) / 999, 2)


def sell_price(item_id: str, day: str) -> int:
    return max(1, int(ITEMS[item_id]["price"] * price_mult(item_id, day)))


def plot_cost(owned: int) -> int:
    """Цена следующей грядки (owned — сколько уже есть)."""
    return 25 * (owned - START_PLOTS + 1) + 15


def capacity_cost(capacity: int) -> int:
    return 80 + (capacity - START_CAPACITY) // CAPACITY_STEP * 70
