"""Учебные материалы для тестов — создаются на лету, чтобы тесты не зависели от файлов из папки test_files (её нет в GitHub)."""
import pymupdf

TOPICS = [
    ("Нейронные сети", ["нейрон", "вес", "функция активации", "слой", "ошибка", "градиент", "обучающая выборка", "эпоха", "вход", "выход"]),
    ("Обработка текста", ["токен", "словарь", "корпус", "вектор", "частота", "стоп-слово", "лемма", "контекст", "признак", "классификатор"]),
    ("Базы данных", ["таблица", "строка", "ключ", "индекс", "запрос", "транзакция", "связь", "схема", "представление", "кортеж"]),
]
SENTENCES = [
    "{t} — одно из ключевых понятий раздела «{topic}»: оно помогает описать, как устроена система и почему она работает именно так.",
    "Чтобы понять {t}, полезно разобрать небольшой пример и посмотреть, что меняется, когда значение {u} увеличивается или уменьшается.",
    "На практике {t} почти всегда рассматривают вместе с понятием {u}, потому что одно определяется через другое.",
    "Типичная ошибка новичка — путать {t} и {u}: первое описывает устройство, а второе — то, как оно используется в расчётах.",
    "Для проверки себя ответь на вопрос: что произойдёт с результатом, если убрать {t}, но оставить {u} без изменений?",
]


def text_for(topic_idx: int, paragraphs: int = 32) -> str:
    topic, terms = TOPICS[topic_idx % len(TOPICS)]
    out = [f"Тема: {topic}\n"]
    for i in range(paragraphs):
        if i % 4 == 0:
            out.append(f"\n{i // 4 + 1}. Раздел {i // 4 + 1}: {terms[(i // 4) % len(terms)].capitalize()}\n")
        t, u = terms[i % len(terms)], terms[(i * 3 + 1) % len(terms)]
        out.append(" ".join(s.format(t=t, u=u, topic=topic) for s in SENTENCES[i % 2:i % 2 + 3]) + f" (пункт {i + 1})\n")
    return "\n".join(out)


def english_pdf(paragraphs: int = 40) -> bytes:
    """Небольшой PDF с английским текстом (проверка разбора PDF; базовый шрифт PDF не содержит кириллицы)."""
    words = ("The neuron computes a weighted sum of its inputs and passes it through an activation function. "
             "Training adjusts the weights to reduce the error on the examples. ")
    doc = pymupdf.open()
    for page_no in range(max(1, paragraphs // 10)):
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(50, 50, 545, 790), f"Lecture part {page_no + 1}\n\n" + words * 12, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def sample_files() -> list[tuple[str, bytes, str]]:
    """Три файла: два текстовых по-русски и один PDF."""
    return [
        ("Лекция 1 — Нейронные сети.txt", text_for(0).encode("utf-8"), "text/plain"),
        ("Лекция 2 — Обработка текста.md", text_for(1).encode("utf-8"), "text/markdown"),
        ("Lecture 3.pdf", english_pdf(), "application/pdf"),
    ]
