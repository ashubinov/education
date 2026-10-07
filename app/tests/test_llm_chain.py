"""Проверка клиента LLM на фейковом OpenRouter: выбор моделей, 429, битый JSON, <think>, ошибки ключа.
Запуск: LQ_DB=<tmp.db> python tests/test_llm_chain.py"""
import asyncio
import json
import os
import sys

import httpx

sys.path.insert(0, ".")
os.environ["OPENROUTER_API_KEY"] = "sk-test"
os.environ.pop("LLM_MOCK", None)
from server import llm as L  # noqa: E402

calls = []


def handler_factory(script):
    def handler(request: httpx.Request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [
                {"id": "qwen/qwen3-235b-a22b:free", "context_length": 40000},
                {"id": "deepseek/deepseek-r1-distill-llama-70b:free", "context_length": 60000},
                {"id": "deepseek/deepseek-chat-v3.1:free", "context_length": 64000},
                {"id": "qwen/qwen2.5-vl-72b-instruct:free", "context_length": 32000},
                {"id": "deepseek/deepseek-r1-0528:free", "context_length": 64000},
                {"id": "openai/gpt-4o", "context_length": 128000},
                {"id": "deepseek/deepseek-chat", "context_length": 64000},
            ]})
        body = json.loads(request.content)
        calls.append((body["model"], bool(body.get("response_format"))))
        assert request.headers["authorization"] == "Bearer sk-test"
        step = script.pop(0)
        if isinstance(step, int):
            return httpx.Response(step, json={"error": {"message": "x"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": step}}]})
    return handler


async def run(script, validate=None):
    calls.clear()
    L.TRANSPORT = httpx.MockTransport(handler_factory(script))
    L.llm._models = []
    L.llm._cool.clear()
    return await L.llm.chat_json("sys", "user", task="t", validate=validate)


async def main():
    ms = await (lambda: (setattr(L, "TRANSPORT", httpx.MockTransport(handler_factory([]))), L.llm.models())[1])()
    print("models:", ms)
    assert ms[0] == "deepseek/deepseek-chat-v3.1:free" and "openai/gpt-4o" not in ms and not any("vl" in m or "distill" in m for m in ms), ms
    assert ms.index("deepseek/deepseek-r1-0528:free") < ms.index("qwen/qwen3-235b-a22b:free")

    # 1) обычный ответ в code fence с хвостовой запятой и <think>
    r = await run(['<think>размышляю</think>\n```json\n{"a": 1, "b": [1,2,],}\n```'])
    assert r == {"a": 1, "b": [1, 2]}, r
    assert calls == [("deepseek/deepseek-chat-v3.1:free", True)], calls

    # 2) 429 на первой модели -> вторая
    r = await run([429, '{"ok": true}'])
    assert r == {"ok": True} and calls[0][0] != calls[1][0], calls

    # 3) невалидный JSON -> просьба исправить -> успех на той же модели
    r = await run(["вот ответ: не JSON", '{"fixed": 1}'])
    assert r == {"fixed": 1} and calls[0][0] == calls[1][0], calls

    # 4) валидатор отклоняет -> повтор -> принят
    def v(o):
        if "x" not in o:
            raise ValueError("нет x")
        return o
    r = await run(['{"y": 1}', '{"x": 2}'], validate=v)
    assert r == {"x": 2}, r

    # 5) все модели падают -> LLMError
    try:
        await run([500] * 10)
        raise SystemExit("ожидалась ошибка")
    except L.LLMError as e:
        assert "Не удалось" in str(e), e

    # 6) неверный ключ -> LLMUnavailable сразу
    try:
        await run([401])
        raise SystemExit("ожидалась LLMUnavailable")
    except L.LLMUnavailable as e:
        print("401 ->", e)

    # 7) 400 на response_format -> повтор без него
    r = await run([400, '{"ok": 1}'])
    assert r == {"ok": 1} and calls[0][1] is True and calls[1][1] is False, calls
    print("OK: llm chain")


asyncio.run(main())
