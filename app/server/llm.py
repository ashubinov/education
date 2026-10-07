"""Клиент LLM через открытое API OpenRouter: бесплатные DeepSeek -> Qwen, с запасными моделями."""
import asyncio
import json
import os
import re
import time

import httpx

from . import config
from .db import db


TRANSPORT = None  # httpx-транспорт для тестов (MockTransport)


class LLMError(Exception):
    pass


class LLMUnavailable(LLMError):
    """Нет ключа или все модели недоступны."""


def _rank(model_id: str) -> int:
    m = model_id.lower()
    if "deepseek" in m:
        if "chat" in m or "v3" in m:
            return 0
        if "r1" in m and "distill" not in m:
            return 2
        return 3
    if "qwen" in m:
        if "coder" in m or "vl" in m:
            return 6
        if "235b" in m or "next" in m or "80b" in m or "instruct" in m:
            return 4
        return 5
    return 9


class LLM:
    def __init__(self):
        self._models: list[str] = []
        self._models_at = 0.0
        self._cool: dict[str, float] = {}
        self._sem = asyncio.Semaphore(2)
        self.last_model = ""
        self.stats = {"calls": 0, "errors": 0}

    # ---------- настройки ----------
    def api_key(self) -> str:
        return (db.get_setting("openrouter_key") or config.env("OPENROUTER_API_KEY")).strip()

    def mock(self) -> bool:
        return config.env("LLM_MOCK") == "1" or db.get_setting("llm_mock") == "1"

    def configured(self) -> bool:
        return bool(self.api_key()) or self.mock()

    def forced_models(self) -> list[str]:
        raw = db.get_setting("llm_models") or config.env("LLM_MODELS")
        return [m.strip() for m in raw.split(",") if m.strip()]

    async def models(self) -> list[str]:
        forced = self.forced_models()
        if forced:
            return forced
        if self._models and time.time() - self._models_at < 3600:
            return self._models
        found: list[str] = []
        try:
            async with httpx.AsyncClient(timeout=20, transport=TRANSPORT) as c:
                r = await c.get(f"{config.OPENROUTER_URL}/models")
                r.raise_for_status()
                for m in r.json().get("data", []):
                    mid = m.get("id", "")
                    if mid.endswith(":free") and ("deepseek" in mid or "qwen" in mid):
                        if any(x in mid for x in ("vl", "coder", "distill", "embed", "guard")):
                            continue
                        ctx = m.get("context_length") or 0
                        if ctx and ctx < 16000:
                            continue
                        found.append(mid)
        except Exception:
            pass
        found.sort(key=lambda x: (_rank(x), x))
        self._models = (found[:8] or list(config.DEFAULT_MODELS))
        self._models_at = time.time()
        return self._models

    # ---------- вызовы ----------
    async def _call(self, model: str, messages: list[dict], *, temperature: float, max_tokens: int, json_mode: bool) -> str:
        body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self.api_key()}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:8000",
            "X-Title": "LearnQuest",
        }
        timeout = httpx.Timeout(connect=15, read=170, write=30, pool=30)
        async with httpx.AsyncClient(timeout=timeout, transport=TRANSPORT) as c:
            r = await c.post(f"{config.OPENROUTER_URL}/chat/completions", json=body, headers=headers)
        if r.status_code == 400 and json_mode:
            # часть провайдеров не поддерживает response_format
            return await self._call(model, messages, temperature=temperature, max_tokens=max_tokens, json_mode=False)
        if r.status_code in (401, 403):
            raise LLMUnavailable("OpenRouter отклонил ключ API (проверьте ключ в настройках)")
        if r.status_code == 402:
            raise LLMUnavailable("На OpenRouter нет средств/лимита для этой модели")
        if r.status_code >= 400:
            raise LLMError(f"{model}: HTTP {r.status_code} {r.text[:200]}")
        data = r.json()
        if data.get("error"):
            raise LLMError(f"{model}: {str(data['error'])[:200]}")
        try:
            msg = data["choices"][0]["message"]["content"] or ""
        except Exception:
            raise LLMError(f"{model}: пустой ответ")
        msg = re.sub(r"<think>.*?</think>", "", msg, flags=re.S).strip()
        if not msg:
            raise LLMError(f"{model}: пустой ответ")
        return msg

    async def chat_json(self, system: str, user: str, *, task: str, ctx: dict | None = None,
                        validate=None, max_tokens: int = 4500, temperature: float = 0.35) -> dict:
        """Запросить JSON. validate(obj) -> obj|raises ValueError. Перебирает модели и пробует починить ответ."""
        if self.mock():
            from . import llm_mock
            await asyncio.sleep(0.15)
            obj = llm_mock.respond(task, ctx or {})
            return validate(obj) if validate else obj
        if not self.api_key():
            raise LLMUnavailable("Не задан ключ OpenRouter. Добавьте его в настройках или в файле .env")

        models = await self.models()
        now = time.time()
        order = [m for m in models if self._cool.get(m, 0) < now] or models
        last_err: Exception | None = None
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        async with self._sem:
            for model in order[:5]:
                raw = ""
                for attempt in range(2):
                    try:
                        self.stats["calls"] += 1
                        raw = await self._call(model, messages, temperature=temperature,
                                               max_tokens=max_tokens, json_mode=True)
                        obj = extract_json(raw)
                        if validate:
                            obj = validate(obj)
                        self.last_model = model
                        return obj
                    except LLMUnavailable:
                        raise
                    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
                        last_err = e
                        self.stats["errors"] += 1
                        # просим модель исправить формат
                        messages = messages[:2] + [
                            {"role": "assistant", "content": raw[:3000] or "{}"},
                            {"role": "user", "content": f"Ответ не прошёл проверку: {str(e)[:300]}. "
                                                         "Верни ТОЛЬКО исправленный валидный JSON, без пояснений."},
                        ]
                    except (LLMError, httpx.HTTPError, asyncio.TimeoutError) as e:
                        last_err = e
                        self.stats["errors"] += 1
                        self._cool[model] = time.time() + 90
                        if "429" in str(e):
                            await asyncio.sleep(2)
                        break  # следующая модель
                messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        raise LLMError(f"Не удалось получить ответ модели: {last_err}")


def extract_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S).strip()
    start = t.find("{")
    end = t.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("в ответе нет JSON-объекта")
    t = t[start:end + 1]
    try:
        return json.loads(t, strict=False)  # strict=False: модели часто кладут «живые» переводы строк внутрь строк
    except json.JSONDecodeError:
        t2 = re.sub(r",\s*([}\]])", r"\1", t)  # хвостовые запятые
        return json.loads(t2, strict=False)


llm = LLM()
