"""Проверка клиента LLM на фейковых OpenRouter / OpenAI-совместимом API: выбор и порядок моделей, 429, 402,
битый JSON, <think>, ошибки ключа, свой провайдер, платный режим.
Запуск: LQ_DB=<tmp.db> python tests/test_llm_chain.py"""
import asyncio
import json
import os
import sys

import httpx

sys.path.insert(0, ".")
os.environ["OPENROUTER_API_KEY"] = "sk-test"
os.environ["DEEPSEEK_API_KEY"] = ""
for k in ("LLM_MOCK", "LLM_BASE_URL", "LLM_API_KEY", "LLM_CUSTOM_MODELS", "LLM_MODELS", "LLM_ALLOW_PAID"):
    os.environ.pop(k, None)
from server import llm as L  # noqa: E402
from server.db import db  # noqa: E402

calls = []

CATALOG = [
    {"id": "nvidia/nemotron-3-ultra-550b-a55b:free", "context_length": 1000000},
    {"id": "google/gemma-4-31b-it:free", "context_length": 262144},
    {"id": "nvidia/nemotron-3-super-120b-a12b:free", "context_length": 262144},
    {"id": "nvidia/nemotron-3.5-content-safety:free", "context_length": 128000},
    {"id": "liquid/lfm-2.5-2.6b:free", "context_length": 65536},
    {"id": "thinkingmachines/inkling-small:free", "context_length": 1048576},
    {"id": "poolside/laguna-s-2.1:free", "context_length": 262144},
    {"id": "deepseek/deepseek-v4-pro", "context_length": 1048576},
    {"id": "deepseek/deepseek-v3.2", "context_length": 163840},
    {"id": "openai/gpt-4o", "context_length": 128000},
]


def handler_factory(script, catalog=CATALOG):
    def handler(request: httpx.Request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": catalog})
        body = json.loads(request.content)
        calls.append((request.url.host, body["model"], bool(body.get("response_format"))))
        step = script.pop(0)
        if isinstance(step, int):
            return httpx.Response(step, json={"error": {"message": "x"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": step}}]})
    return handler


async def run(script, validate=None, catalog=CATALOG):
    calls.clear()
    L.TRANSPORT = httpx.MockTransport(handler_factory(script, catalog))
    L.llm.reset_cache()
    return await L.llm.chat_json("sys", "user", task="t", validate=validate)


async def main():
    # --- порядок бесплатных моделей: мусор отфильтрован, Nemotron-super первым
    L.TRANSPORT = httpx.MockTransport(handler_factory([]))
    L.llm.reset_cache()
    ms = await L.llm.models()
    print("free chain:", ms)
    assert ms[0] == "nvidia/nemotron-3-super-120b-a12b:free", ms
    assert all(m.endswith(":free") for m in ms) and not any(x in " ".join(ms) for x in ("safety", "lfm", "small", "gpt-4o", "deepseek-v4")), ms
    assert "google/gemma-4-31b-it:free" in ms and "poolside/laguna-s-2.1:free" in ms

    # --- если вернутся бесплатные DeepSeek/Qwen — они обязаны идти первыми
    cat2 = CATALOG + [{"id": "qwen/qwen3-235b-a22b:free", "context_length": 40000}, {"id": "deepseek/deepseek-chat-v3.1:free", "context_length": 64000},
                      {"id": "deepseek/deepseek-r1-0528:free", "context_length": 64000}, {"id": "qwen/qwen3-coder:free", "context_length": 64000}]
    L.TRANSPORT = httpx.MockTransport(handler_factory([], cat2))
    L.llm.reset_cache()
    ms2 = await L.llm.models()
    assert ms2[0] == "deepseek/deepseek-chat-v3.1:free" and ms2.index("deepseek/deepseek-r1-0528:free") < ms2.index("qwen/qwen3-235b-a22b:free") < ms2.index("nvidia/nemotron-3-super-120b-a12b:free"), ms2
    assert "qwen/qwen3-coder:free" not in ms2

    # --- платный режим: дешёвые DeepSeek впереди бесплатных
    db.set_setting("llm_paid", "1")
    L.llm.reset_cache()
    ms3 = await L.llm.models()
    assert ms3[:2] == ["deepseek/deepseek-v4-pro", "deepseek/deepseek-v3.2"] and ms3[2].endswith(":free"), ms3
    db.set_setting("llm_paid", "0")

    # 1) ответ в code fence с хвостовой запятой и <think>
    r = await run(['<think>размышляю</think>\n```json\n{"a": 1, "b": [1,2,],}\n```'])
    assert r == {"a": 1, "b": [1, 2]} and calls == [("openrouter.ai", "nvidia/nemotron-3-super-120b-a12b:free", True)], calls

    # 2) 429 на первой модели -> вторая
    r = await run([429, '{"ok": true}'])
    assert r == {"ok": True} and calls[0][1] != calls[1][1], calls

    # 3) невалидный JSON -> просьба исправить -> успех на той же модели
    r = await run(["вот ответ: не JSON", '{"fixed": 1}'])
    assert r == {"fixed": 1} and calls[0][1] == calls[1][1], calls

    # 4) валидатор отклоняет -> повтор -> принят
    def v(o):
        if "x" not in o:
            raise ValueError("нет x")
        return o
    assert await run(['{"y": 1}', '{"x": 2}'], validate=v) == {"x": 2}

    # 5) переносы строк внутри строк JSON
    assert (await run(['{"t": "строка 1\nстрока 2"}']))["t"].count("\n") == 1

    # 6) все модели падают -> LLMError
    try:
        await run([500] * 20)
        raise SystemExit("ожидалась ошибка")
    except L.LLMError as e:
        assert "Не удалось" in str(e), e

    # 7) неверный ключ -> LLMUnavailable
    try:
        await run([401] * 10)
        raise SystemExit("ожидалась LLMUnavailable")
    except L.LLMUnavailable as e:
        print("401 ->", e)

    # 8) 400 на response_format -> повтор без него
    r = await run([400, '{"ok": 1}'])
    assert r == {"ok": 1} and calls[0][2] is True and calls[1][2] is False, calls

    # 9) 402 (нет баланса) -> следующая модель, и эта модель надолго «остывает»
    db.set_setting("llm_paid", "1")
    r = await run([402, '{"ok": 3}'])
    assert calls[0][1] == "deepseek/deepseek-v4-pro" and calls[1][1] == "deepseek/deepseek-v3.2" and r == {"ok": 3}, calls
    assert L.llm._cool["openrouter|deepseek/deepseek-v4-pro"] - __import__("time").time() > 1000
    db.set_setting("llm_paid", "0")

    # 10) свой провайдер (DeepSeek напрямую) идёт первым; при его падении — OpenRouter
    db.set_setting("llm_base_url", "https://api.deepseek.com")
    db.set_setting("llm_api_key", "dk-test")
    db.set_setting("llm_custom_models", "deepseek-chat")
    r = await run(['{"who": "deepseek"}'])
    assert calls == [("api.deepseek.com", "deepseek-chat", True)] and r == {"who": "deepseek"}, calls
    r = await run([500, '{"who": "openrouter"}'])
    assert calls[0][0] == "api.deepseek.com" and calls[1][0] == "openrouter.ai" and r == {"who": "openrouter"}, calls
    # ключ своего провайдера отклонён -> уходим на OpenRouter, не падаем
    r = await run([401, '{"who": "or2"}'])
    assert calls[1][0] == "openrouter.ai" and r == {"who": "or2"}, calls
    for k in ("llm_base_url", "llm_api_key", "llm_custom_models"):
        db.set_setting(k, "")

    print("OK: llm chain")


asyncio.run(main())
