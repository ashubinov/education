"""Живая проверка: прогнать РЕАЛЬНЫЙ промпт «структура курса» по тестовым PDF на указанных моделях OpenRouter.
Использование: python tests/live_try.py <модель> [<модель> ...]   (нужен OPENROUTER_API_KEY в .env)
Каждая модель = 1 запрос к API."""
import asyncio
import glob
import sys
import time

sys.path.insert(0, ".")
from server import generator, ingest, prompts  # noqa: E402
from server.llm import LLMError, extract_json, llm  # noqa: E402


async def main(models):
    texts = []
    for p in sorted(glob.glob("../test_files/*.pdf")):
        texts.append((p.split("/")[-1].split("\\")[-1], ingest.extract_text(p, open(p, "rb").read())))
    chunks = []
    for name, t in texts:
        chunks += ingest.chunk_text(t)
    material = "\n\n".join(f"[#{i}]\n{c}" for i, c in enumerate(chunks, 1))
    print(f"фрагментов: {len(chunks)}, символов: {len(material)}")
    system = prompts.system("ru")
    user = prompts.outline(material, [n for n, _ in texts], "", True)
    for m in models:
        t0 = time.time()
        try:
            raw = await llm._call(m, [{"role": "system", "content": system}, {"role": "user", "content": user}],
                                  temperature=0.35, max_tokens=4000, json_mode=True)
            dt = time.time() - t0
            obj = generator.norm_outline(extract_json(raw), len(chunks))
            print(f"\n== {m}: OK за {dt:.0f} с, модулей {len(obj['modules'])}, практика={obj['has_practice']}/{obj['practice_kind']}")
            print("   курс:", obj["title"], "|", obj["description"][:100])
            for mod in obj["modules"]:
                print("   -", mod["title"], "| чанки:", mod["chunk_ids"], "| практ.:", mod["practical"])
        except Exception as e:
            print(f"\n== {m}: ОШИБКА за {time.time() - t0:.0f} с -> {type(e).__name__}: {str(e)[:300]}")


asyncio.run(main(sys.argv[1:]))
