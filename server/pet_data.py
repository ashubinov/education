"""Данные питомцев: виды животных, еда, этапы роста. Всё придумано для LearnQuest.

Сытость падает со временем (считается лениво по времени, фоновых задач нет). Еда стоит жетонов; любимая еда вида даёт больше роста.
Этапы роста: малыш → подросток → взрослый; питомец никогда не умирает — если забросить, он просто грустит и не растёт.
"""

# сколько единиц сытости теряется за час (из 100): пустой желудок примерно через сутки
DECAY_PER_HOUR = 4.2
FULL = 100
CANT_EAT_ABOVE = 90      # сытый питомец отказывается есть
START_FULLNESS = 75
LIKED_BONUS = 1.5        # любимая еда даёт на 50 % больше роста
STAGES = (("baby", "Малыш", 0), ("teen", "Подросток", 220), ("adult", "Взрослый", 900))
NAME_MAX = 16
PAT_REWARD = 3           # жетонов тому, кто погладил чужого питомца (раз в день на одного друга)
BUY_MAX_QTY = 20

# вид: название, любимые категории еды
SPECIES = {
    "corgi": {"name": "Корги", "likes": ["dairy", "bone"], "starter": "cheese", "desc": "Любит молочное и косточки."},
    "doberman": {"name": "Доберман", "likes": ["meat", "bone"], "starter": "bone", "desc": "Любит мясо и косточки."},
    "bulldog": {"name": "Бульдог", "likes": ["meat", "dairy"], "starter": "meat", "desc": "Любит мясо и молочное."},
    "pug": {"name": "Мопс", "likes": ["bone", "sweet"], "starter": "cookie", "desc": "Любит косточки и сладкое."},
    "chihuahua": {"name": "Чихуахуа", "likes": ["meat", "sweet"], "starter": "cookie", "desc": "Любит мясо и сладкое."},
    "rooster": {"name": "Петух", "likes": ["grain", "veg"], "starter": "grain", "desc": "Любит зерно и овощи."},
    "ram": {"name": "Кучерявый баран", "likes": ["grass", "veg"], "starter": "hay", "desc": "Любит сено и овощи."},
    "beaver": {"name": "Бобр", "likes": ["wood", "veg"], "starter": "twig", "desc": "Любит ветки и овощи."},
}

# еда: id → (название, значок, цена в жетонах, сытость, рост, категория)
FOODS_RAW = [
    ("hay", "Сено", "🌿", 5, 10, 3, "grass"),
    ("grain", "Зерно", "🌾", 6, 12, 4, "grain"),
    ("twig", "Ветки", "🪵", 8, 14, 5, "wood"),
    ("carrot", "Морковка", "🥕", 5, 10, 3, "veg"),
    ("corn", "Кукуруза", "🌽", 6, 12, 4, "veg"),
    ("milk", "Молоко", "🥛", 8, 15, 4, "dairy"),
    ("cheese", "Сыр", "🧀", 15, 22, 8, "dairy"),
    ("cookie", "Печенье", "🍪", 10, 12, 5, "sweet"),
    ("honey", "Мёд", "🍯", 25, 25, 12, "sweet"),
    ("bone", "Косточка", "🦴", 20, 30, 12, "bone"),
    ("meat", "Мясо", "🍗", 28, 35, 15, "meat"),
    ("cake", "Тортик", "🍰", 40, 40, 20, "sweet"),
    ("steak", "Стейк", "🥩", 60, 60, 30, "meat"),
]
FOODS = {f[0]: {"id": f[0], "name": f[1], "icon": f[2], "price": f[3], "fill": f[4], "xp": f[5], "cat": f[6]} for f in FOODS_RAW}


def liked(species: str, food_id: str) -> bool:
    return FOODS[food_id]["cat"] in SPECIES[species]["likes"] or food_id == SPECIES[species]["starter"]


def stage_of(xp: int) -> dict:
    cur = STAGES[0]
    for st in STAGES:
        if xp >= st[2]:
            cur = st
    idx = STAGES.index(cur)
    nxt = STAGES[idx + 1] if idx + 1 < len(STAGES) else None
    return {"id": cur[0], "name": cur[1], "index": idx, "from": cur[2], "next": nxt[2] if nxt else None, "next_name": nxt[1] if nxt else None}


def mood_of(fullness: float) -> str:
    if fullness >= 60:
        return "happy"
    if fullness >= 30:
        return "ok"
    if fullness > 0:
        return "hungry"
    return "starving"


MOOD_NAMES = {"happy": "Счастлив", "ok": "Нормально", "hungry": "Проголодался", "starving": "Очень голоден"}
