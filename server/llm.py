"""Клиент LLM через открытые OpenAI-совместимые API.

Цепочка моделей (перебираются по порядку, пока одна не вернёт валидный ответ):
  1. свой провайдер (LLM_BASE_URL + LLM_API_KEY + LLM_CUSTOM_MODELS) — например, DeepSeek напрямую, Cerebras, Groq;
  2. OpenRouter: если разрешены платные — дешёвые DeepSeek/Qwen; затем лучшие БЕСПЛАТНЫЕ модели каталога
     (DeepSeek и Qwen идут первыми, когда они там есть; сейчас в бесплатном каталоге — Nemotron, Gemma и др.).
"""
import asyncio
import json
import pathlib
import re
import time

import httpx

from . import config
from .db import db

TRANSPORT = None  # httpx-транспорт для тестов (MockTransport)


class LLMError(Exception):
    pass


class LLMUnavailable(LLMError):
    """Нет ключа или ключ отклонён всеми провайдерами."""


class _AuthError(LLMError):
    pass


class ManualNeeded(LLMUnavailable):
    """Ручной режим (LLM_MANUAL_DIR): для этого запроса ещё нет файла-ответа. Промпт сохранён рядом."""


# ---- ранжирование бесплатных моделей OpenRouter ----
FREE_PREF = [r"deepseek", r"qwen", r"nemotron-3-(ultra|super)", r"gemma-4-31b", r"laguna-s", r"gemma-4-26b", r"ling-"]
FREE_BLOCK = re.compile(r"(inkling|safety|guard|embed|vision|-vl|vl-|audio|omni|lfm|nano|coder|code|small|mini|tiny|note|moderation|distill|reasoning)")
# платные, но очень дешёвые (центы за курс) — только если включено в настройках
PAID_PREF = ["deepseek/deepseek-v4-pro", "deepseek/deepseek-v3.2", "deepseek/deepseek-chat-v3.1", "qwen/qwen3-235b-a22b-2507"]


def _free_rank(model_id: str) -> tuple:
    m = model_id.lower()
    for i, pat in enumerate(FREE_PREF):
        if re.search(pat, m):
            sub = 0
            if "deepseek" in m:
                sub = 0 if ("chat" in m or "v3" in m) else 2 if "r1" in m else 3
            elif "qwen" in m:
                sub = 0 if any(x in m for x in ("235b", "next", "80b", "instruct")) else 1
            return (i, sub, m)
    return (50, 0, m)


class LLM:
    def __init__(self):
        self._or_models: list[str] = []
        self._or_at = 0.0
        self._cool: dict[str, float] = {}
        self._sem = asyncio.Semaphore(2)
        self.last_model = ""
        self.stats = {"calls": 0, "errors": 0}
        self.catalog_ok = True

    # ---------- настройки ----------
    def api_key(self) -> str:
        return (db.get_setting("openrouter_key") or config.env("OPENROUTER_API_KEY")).strip()

    def deepseek(self) -> dict | None:
        """DeepSeek напрямую (DEEPSEEK_API_KEY) — первый в цепочке."""
        key = (db.get_setting("deepseek_key") or config.env("DEEPSEEK_API_KEY")).strip()
        if not key:
            return None
        models = [m.strip() for m in (config.env("DEEPSEEK_MODELS") or "deepseek-chat").split(",") if m.strip()]
        return {"name": "deepseek", "base": (config.env("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/"), "key": key, "models": models}

    def custom(self) -> dict | None:
        base = (db.get_setting("llm_base_url") or config.env("LLM_BASE_URL")).strip().rstrip("/")
        models = [m.strip() for m in (db.get_setting("llm_custom_models") or config.env("LLM_CUSTOM_MODELS")).split(",") if m.strip()]
        if not base or not models:
            return None
        return {"name": "custom", "base": base, "key": (db.get_setting("llm_api_key") or config.env("LLM_API_KEY")).strip(), "models": models}

    def mock(self) -> bool:
        return config.env("LLM_MOCK") == "1" or db.get_setting("llm_mock") == "1"

    def manual_dir(self):
        d = config.env("LLM_MANUAL_DIR")
        return pathlib.Path(d) if d else None

    def configured(self) -> bool:
        return self.manual_dir() is not None or bool(self.api_key()) or bool(self.custom()) or bool(self.deepseek()) or self.mock()

    def paid_allowed(self) -> bool:
        return (db.get_setting("llm_paid") or config.env("LLM_ALLOW_PAID")) in ("1", "true", "yes")

    def forced_models(self) -> list[str]:
        raw = db.get_setting("llm_models") or config.env("LLM_MODELS")
        return [m.strip() for m in raw.split(",") if m.strip()]

    def reset_cache(self):
        self._or_models, self._or_at = [], 0.0
        self._cool.clear()

    # ---------- каталог OpenRouter ----------
    async def models(self) -> list[str]:
        """Упорядоченный список моделей OpenRouter (без своего провайдера)."""
        forced = self.forced_models()
        if forced:
            return forced
        paid = self.paid_allowed()
        if self._or_models and time.time() - self._or_at < 3600:
            return self._or_models
        free: list[str] = []
        ids: set[str] = set()
        try:
            async with httpx.AsyncClient(timeout=20, transport=TRANSPORT) as c:
                r = await c.get(f"{config.OPENROUTER_URL}/models")
                r.raise_for_status()
                for m in r.json().get("data", []):
                    mid = m.get("id", "")
                    ids.add(mid)
                    if not mid.endswith(":free") or FREE_BLOCK.search(mid.lower()):
                        continue
                    ctx = m.get("context_length") or 0
                    if ctx and ctx < 32000:
                        continue
                    free.append(mid)
            self.catalog_ok = True
        except Exception:
            self.catalog_ok = False
        free.sort(key=_free_rank)
        out = []
        if paid:
            out += [m for m in PAID_PREF if m in ids or not ids]
        out += free[:6] or list(config.DEFAULT_MODELS)
        self._or_models = out
        self._or_at = time.time()
        return out

    async def chain(self) -> list[tuple[str, str]]:
        """Полная цепочка: [(провайдер, модель), ...]: DeepSeek напрямую → свой провайдер → OpenRouter."""
        out: list[tuple[str, str]] = []
        for name, getter in (("deepseek", self.deepseek), ("custom", self.custom)):
            pv = getter()
            if pv:
                out += [(name, m) for m in pv["models"]]
        if self.api_key():
            out += [("openrouter", m) for m in await self.models()]
        return out

    def _provider(self, name: str) -> dict:
        if name in ("custom", "deepseek"):
            pv = self.custom() if name == "custom" else self.deepseek()
            return {"base": pv["base"], "key": pv["key"], "headers": {}}
        return {"base": config.OPENROUTER_URL, "key": self.api_key(),
                "headers": {"HTTP-Referer": "http://localhost:8000", "X-Title": "LearnQuest"}}

    # ---------- вызовы ----------
    async def _call(self, provider: str, model: str, messages: list[dict], *, temperature: float, max_tokens: int, json_mode: bool, plain: bool = False) -> str:
        pv = self._provider(provider)
        body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if provider == "openrouter" and not plain:
            # рассуждающие модели (Nemotron и др.) тратят токены на «мысли»: даём запас и просим думать поменьше
            body["max_tokens"] = max_tokens + 3000
            body["reasoning"] = {"effort": "low"}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json", **pv["headers"]}
        if pv["key"]:
            headers["Authorization"] = f"Bearer {pv['key']}"
        timeout = httpx.Timeout(connect=15, read=170, write=30, pool=30)
        async with httpx.AsyncClient(timeout=timeout, transport=TRANSPORT) as c:
            r = await c.post(f"{pv['base']}/chat/completions", json=body, headers=headers)
        if r.status_code == 400 and (json_mode or "reasoning" in body):
            # часть провайдеров не поддерживает response_format / reasoning — повторяем в упрощённом виде
            return await self._call(provider, model, messages, temperature=temperature, max_tokens=max_tokens, json_mode=False, plain=True)
        if r.status_code == 401:
            raise _AuthError(f"{provider}: ключ API отклонён (401)")
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
        """Запросить JSON. validate(obj) -> obj | ValueError. Перебирает цепочку моделей, просит исправить формат."""
        md = self.manual_dir()
        if md:
            return self._manual(md, system, user, task, ctx or {}, validate)
        if self.mock():
            from . import llm_mock
            await asyncio.sleep(0.15)
            obj = llm_mock.respond(task, ctx or {})
            return validate(obj) if validate else obj
        chain = await self.chain()
        if not chain:
            raise LLMUnavailable("Не задан ключ API. Добавьте ключ OpenRouter в настройках или в файле .env")

        now = time.time()
        order = [e for e in chain if self._cool.get(f"{e[0]}|{e[1]}", 0) < now] or chain
        last_err: Exception | None = None
        auth_only = True
        base_messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        bad_providers: set[str] = set()
        async with self._sem:
            for provider, model in order[:6]:
                if provider in bad_providers:
                    continue
                messages = list(base_messages)
                raw = ""
                for attempt in range(2):
                    try:
                        self.stats["calls"] += 1
                        raw = await self._call(provider, model, messages, temperature=temperature, max_tokens=max_tokens, json_mode=True)
                        obj = extract_json(raw)
                        if validate:
                            obj = validate(obj)
                        self.last_model = model
                        return obj
                    except _AuthError as e:
                        last_err = e
                        bad_providers.add(provider)
                        self._cool[f"{provider}|{model}"] = time.time() + 300
                        break
                    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
                        last_err, auth_only = e, False
                        self.stats["errors"] += 1
                        messages = base_messages + [
                            {"role": "assistant", "content": raw[:3000] or "{}"},
                            {"role": "user", "content": f"Ответ не прошёл проверку: {str(e)[:300]}. Верни ТОЛЬКО исправленный валидный JSON, без пояснений."},
                        ]
                    except (LLMError, httpx.HTTPError, asyncio.TimeoutError) as e:
                        last_err, auth_only = e, False
                        self.stats["errors"] += 1
                        txt = str(e)
                        # 402/404 (нет баланса / модель недоступна) — надолго; 429/5xx — ненадолго
                        self._cool[f"{provider}|{model}"] = time.time() + (3600 if ("HTTP 402" in txt or "HTTP 404" in txt) else 90)
                        if "HTTP 429" in txt:
                            await asyncio.sleep(2)
                        break  # следующая модель
        if auth_only and isinstance(last_err, _AuthError):
            raise LLMUnavailable(f"Ключ API отклонён: {last_err}. Проверьте ключ в настройках.")
        raise LLMError(f"Не удалось получить ответ модели: {last_err}")


def _manual(self, md, system: str, user: str, task: str, ctx: dict, validate):
    """Ручной режим: ответ на промпт даёт человек/ассистент файлом <ключ>.json в каталоге LLM_MANUAL_DIR.
    Промпт (ровно тот, что ушёл бы модели) сохраняется в <ключ>.prompt.txt. Ответ проходит те же проверки, что и ответ модели."""
    md.mkdir(parents=True, exist_ok=True)
    m = ctx.get("module") or {}
    key = task if task == "outline" else f"{task}__m{m.get('idx', 0)}"
    ans = md / f"{key}.json"
    if not ans.exists():
        (md / f"{key}.prompt.txt").write_text(f"=== SYSTEM ===\n{system}\n\n=== USER ===\n{user}\n", encoding="utf-8")
        raise ManualNeeded(f"MANUAL:{key}")
    try:
        obj = extract_json(ans.read_text(encoding="utf-8"))
        return validate(obj) if validate else obj
    except (ValueError, KeyError, TypeError) as e:
        raise LLMError(f"MANUAL-INVALID:{key}: {e}")


LLM._manual = _manual


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
