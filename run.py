"""Запуск LearnQuest: `python run.py` (или start.bat). Адрес и порт — из .env (HOST, PORT)."""
import threading
import webbrowser

import uvicorn

from server import config


def main():
    host_for_browser = "127.0.0.1" if config.HOST in ("0.0.0.0", "") else config.HOST
    url = f"http://{host_for_browser}:{config.PORT}"
    print(f"\n  LearnQuest запущен: {url}\n  (остановить: Ctrl+C)\n")
    if config.env("NO_BROWSER") != "1":
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("server.main:app", host=config.HOST, port=config.PORT, log_level="info")


if __name__ == "__main__":
    main()
