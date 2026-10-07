"""Детерминированная «модель» для разработки и демо без ключа API (LLM_MOCK=1).
Строит правдоподобный контент из реального текста материалов — чтобы проверять весь поток приложения.
"""
import random
import re
from collections import Counter

WORD = re.compile(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\-]{4,}")
STOP = {"который", "которые", "которая", "также", "только", "можно", "будет", "между", "поэтому", "каждый", "ученик",
        "слайд", "дополнение", "материалов", "лекция", "занятие", "основной", "ключевые", "определения", "теоремы"}


def _sentences(text: str) -> list[str]:
    t = re.sub(r"\s+", " ", text)
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", t) if 30 <= len(s.strip()) <= 260]


def _terms(text: str, n: int) -> list[str]:
    c = Counter(w.lower() for w in WORD.findall(text) if w.lower() not in STOP)
    caps = [w for w in WORD.findall(text) if w.isupper() and len(w) >= 3]
    names = list(dict.fromkeys(caps))[:3]
    names += [w for w, _ in c.most_common(30) if w not in [x.lower() for x in names]]
    out, seen = [], set()
    for w in names:
        k = w.lower()
        if k not in seen:
            seen.add(k)
            out.append(w if w.isupper() else w.capitalize())
        if len(out) >= n:
            break
    return out


def _sent_for(term: str, text: str) -> str:
    for s in _sentences(text):
        if term.lower() in s.lower():
            return s
    ss = _sentences(text)
    return ss[0] if ss else f"{term} — важное понятие этого раздела."


def _svg(label_a: str, label_b: str) -> str:
    return (f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 400 240'><rect width='400' height='240' fill='#f7f7fb' rx='12'/>"
            f"<rect x='40' y='90' width='120' height='60' rx='10' fill='#c7d2fe' stroke='#4f46e5'/>"
            f"<text x='100' y='125' font-size='15' text-anchor='middle' fill='#222'>{label_a}</text>"
            f"<line x1='160' y1='120' x2='240' y2='120' stroke='#4f46e5' stroke-width='3'/>"
            f"<polygon points='240,110 260,120 240,130' fill='#4f46e5'/>"
            f"<rect x='260' y='90' width='110' height='60' rx='10' fill='#bbf7d0' stroke='#16a34a'/>"
            f"<text x='315' y='125' font-size='15' text-anchor='middle' fill='#222'>{label_b}</text></svg>")


def respond(task: str, ctx: dict) -> dict:
    rng = random.Random(len(str(ctx)))
    if task == "digest":
        out = []
        for idx, text in ctx["chunks"]:
            ts = _terms(text, 5)
            out.append({"id": idx, "summary": (_sentences(text) or [text[:120]])[0], "terms": ts,
                        "practice": bool(re.search(r"задач|упражн|def |пример", text.lower()))})
        return {"digests": out}

    if task == "outline":
        chunks = ctx["chunks"]
        n = len(chunks)
        k = max(2, min(5, (n + 1) // 2)) if n > 1 else 1
        per = max(1, -(-n // k))
        mods = []
        for i in range(0, n, per):
            grp = chunks[i:i + per]
            text = " ".join(t for _, t in grp)
            ts = _terms(text, 3)
            mods.append({"title": "Модуль: " + ", ".join(ts[:2]) if ts else f"Модуль {len(mods) + 1}",
                         "summary": (_sentences(text) or [text[:140]])[0], "objectives": [f"Понимать {t}" for t in ts[:3]],
                         "chunk_ids": [c for c, _ in grp], "practical": len(mods) % 2 == 0, "estimated_terms": 6})
        fn = ctx.get("filenames", ["Курс"])[0].rsplit(".", 1)[0]
        return {"title": fn[:60], "description": "Курс, собранный по загруженным материалам (демо-режим).", "icon": "🧠",
                "has_practice": True, "practice_kind": "problems", "modules": mods}

    if task == "supplement":
        out = respond("outline", ctx)
        return {"has_practice": False, "modules": out["modules"][:2]}

    if task == "module_start":
        plan = respond("module_plan", ctx)
        qs = respond("intro_test", {**ctx, "terms": plan["terms"]})
        return {**plan, "questions": qs["questions"]}

    if task == "module_plan":
        ts = _terms(ctx["context"], 7)
        if len(ts) < 3:
            ts += ["Понятие А", "Понятие Б", "Понятие В"]
        return {"terms": [{"term": t, "gist": _sent_for(t, ctx["context"])[:100]} for t in ts[:7]],
                "practice_tasks": 1 if ctx["module"].get("_course_practice") else 0, "focus": "Упор на основные определения."}

    if task == "intro_test":
        terms = ctx["terms"]
        qs = []
        for i, t in enumerate(terms[:8]):
            name = t["term"]
            s = _sent_for(name, ctx["context"])
            others = [x["term"] for x in terms if x["term"] != name]
            if i % 4 == 0 and len(others) >= 3:
                qs.append({"kind": "single", "concept": name, "prompt": f"Что из перечисленного относится к понятию «{name}»?",
                           "correct": s[:110], "wrong": [f"Описание понятия «{o}»" for o in others[:3]], "explanation": s})
            elif i % 4 == 1:
                qs.append({"kind": "fill", "concept": name, "prompt": re.sub(re.escape(name), "___", s, flags=re.I)[:200] if name.lower() in s.lower() else f"Понятие, о котором идёт речь: ___ ({s[:80]})",
                           "accept": [name, name.lower()], "explanation": s})
            elif i % 4 == 2 and len(others) >= 3:
                qs.append({"kind": "multi", "concept": name, "prompt": f"Выберите все верные утверждения о «{name}» (выберите все верные)",
                           "correct": [s[:100], f"«{name}» входит в изучаемую тему"], "wrong": [f"«{name}» не связано с темой", f"Это то же самое, что «{others[0]}»"],
                           "explanation": s})
            else:
                o = others[:1] or ["Другое понятие"]
                qs.append({"kind": "image", "concept": name, "svg": _svg(name[:12], o[0][:12]),
                           "prompt": f"На схеме слева изображено понятие «{name}». Как оно называется?", "correct": name,
                           "wrong": [o[0], "Неизвестный блок", "Выход"], "explanation": s})
        return {"questions": qs}

    if task == "terms":
        out = []
        for t in ctx["terms"]:
            s = _sent_for(t["term"], ctx["context"])
            kp = [w.lower() for w in WORD.findall(s) if w.lower() not in STOP][:4]
            out.append({"term": t["term"], "definition": s, "key_points": kp, "example": f"Пример: {t['term']} встречается в материале.",
                        "wrong_terms": ["Случайный термин", "Другое понятие", "Иная категория"],
                        "wrong_definitions": ["Это набор несвязанных понятий, не относящихся к теме.", "Метод, который не используется в курсе.", "Название файла с материалами."]})
        return {"terms": out}

    if task == "practice":
        ts = [t["term"] for t in ctx["terms"]][:3] or ["понятие"]
        task_obj = {"title": f"Применяем: {ts[0]}", "kind": "problems", "language": "",
                    "statement": f"Объясните своими словами, как связаны понятия {', '.join(ts)}, и приведите один пример применения.",
                    "starter": "", "hints": ["Начните с определения.", "Приведите конкретный пример."],
                    "solution": f"{ts[0]} и остальные понятия связаны через общую тему модуля; пример — применение {ts[0]} на практике.",
                    "rubric": ["Названы все понятия", "Есть связь между ними", "Есть пример"],
                    "methods": [{"name": ts[0], "when": "в начале рассуждения"}]}
        qs = [{"kind": "single", "concept": ts[0], "prompt": "Какой приём стоит применить в первую очередь при решении такой задачи?",
               "correct": f"Опереться на определение «{ts[0]}»", "wrong": ["Угадать ответ", "Пропустить условие", "Сразу писать итог"], "explanation": "Начинаем с определений."}
              for _ in range(3)]
        for i, q in enumerate(qs):
            q["prompt"] += f" (вариант {i + 1})"
        return {"task": task_obj, "questions": qs}

    if task == "final_test":
        terms = ctx["terms"]
        qs = []
        for i, t in enumerate(terms[:8]):
            name = t["term"]
            s = _sent_for(name, ctx["context"])
            others = [x["term"] for x in terms if x["term"] != name]
            if i % 3 == 0:
                qs.append({"kind": "fill", "concept": name, "prompt": f"Термин, который описывается так: «{s[:120]}» — это ___.", "accept": [name], "explanation": s})
            else:
                qs.append({"kind": "single", "concept": name, "prompt": f"Какое утверждение верно для «{name}»? (контроль {i + 1})",
                           "correct": s[:110], "wrong": [f"«{name}» не имеет отношения к теме", f"«{name}» — то же, что «{(others or ['X'])[0]}»", "Ни одно из утверждений не подходит по смыслу"],
                           "explanation": s})
        return {"questions": qs}

    if task == "evaluate":
        ans = ctx.get("answer", "")
        sc = 1.0 if len(ans) > 60 else 0.5 if len(ans) > 15 else 0.0
        return {"score": sc, "verdict": "correct" if sc >= 0.85 else "partial" if sc else "wrong",
                "feedback": "Демо-проверка: засчитано по объёму ответа.", "missing": [] if sc == 1 else ["Раскрой ответ подробнее"], "concepts": []}

    if task == "adapt":
        return {"notes": "Ученик осваивает материал в обычном темпе (демо).", "review": None, "message": "Модуль пройден — так держать!"}
    raise ValueError(f"mock: неизвестная задача {task}")
