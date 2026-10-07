"""Конфигурация: переменные окружения + файл .env (+ настройки из БД, см. db.get_setting)."""
import os
import pathlib

BASE = pathlib.Path(__file__).resolve().parent.parent
def _load_env() -> None:
    for p in (BASE / ".env", BASE.parent / ".env"):
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if v and k not in os.environ:
                os.environ[k] = v


_load_env()

# Каталог данных: локально ./data, в облаке (Amvera) — постоянная папка /data (DATA_DIR=/data)
DATA = (pathlib.Path(os.environ["DATA_DIR"]) if os.environ.get("DATA_DIR")
        else pathlib.Path("/data") if os.environ.get("AMVERA") == "1" else BASE / "data")
UPLOADS = DATA / "uploads"
DATA.mkdir(parents=True, exist_ok=True)
UPLOADS.mkdir(parents=True, exist_ok=True)

FRONT_DIR = pathlib.Path(os.environ["FRONT_DIR"]) if os.environ.get("FRONT_DIR") else BASE / "front"
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
DB_PATH = pathlib.Path(os.environ["LQ_DB"]) if os.environ.get("LQ_DB") else DATA / "learnquest.db"

MAX_UPLOAD_MB = 25
MAX_SOURCE_CHARS = 250_000  # всего текста на один курс (больше — упрёмся в лимиты бесплатного API)
CHUNK_CHARS = 3500
FULL_TEXT_LIMIT = 28_000  # столько текста отправляем в план курса целиком
MODULE_CONTEXT_CHARS = 9_000

OPENROUTER_URL = "https://openrouter.ai/api/v1"
# Запасной список бесплатных моделей, если не удалось получить каталог OpenRouter
DEFAULT_MODELS = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemma-4-31b-it:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "poolside/laguna-s-2.1:free",
]


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)
