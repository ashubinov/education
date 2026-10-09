"""Общее для скриптов сборки каталога: список курсов и порядок файлов."""
import pathlib
import re

COURSES = [  # (номер в каталоге, папка в «курсы/», название)
    (101, "информационные системы поддержки принятия решений", "Информационные системы поддержки принятия решений"),
    (102, "ос", "Операционные системы"),
    (103, "анализ больших данных", "Анализ больших данных"),
]
ORDINALS = {"перв": 1, "втор": 2, "трет": 3, "четверт": 4, "пят": 5, "шест": 6, "седьм": 7, "восьм": 8, "девят": 9, "десят": 10}


def order_key(path: pathlib.Path):
    """Порядок лекций: по номеру в имени («Лекция 2 …», «Лекция_3_…»), иначе по порядковому слову («первая», «второй»)."""
    name = path.name.lower()
    m = re.search(r"(?:лекци[яи]|lecture)[\s_]*(\d+)", name)
    if m:
        return int(m.group(1))
    for part in (name, path.parent.name.lower()):
        for stem, n in ORDINALS.items():
            if stem in part:
                return n
    return 999


def course_files(folder: pathlib.Path):
    files = [p for p in folder.rglob("*") if p.is_file() and not p.name.startswith("~$")]
    return sorted(files, key=lambda p: (order_key(p), p.name))
