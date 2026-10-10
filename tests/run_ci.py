"""Один запуск всех проверок бэкенда (для GitHub Actions и для локальной проверки перед пушем):

    python tests/run_ci.py

Для каждого сквозного теста поднимается свой чистый сервер (мок-ИИ, пустая БД во временной папке, без ключей и токенов), поэтому
проверки не влияют друг на друга и не трогают настоящие данные. Тесты, которым нужен catalog_seed.json (его нет в GitHub), пропускаются.
Работает на Windows, Linux и macOS. Код возврата 0 — всё зелёное, иначе 1.
"""
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
PY = sys.executable
HAS_SEED = (ROOT / "catalog_seed.json").exists()

# (файл, нужен ли свой сервер, нужен ли catalog_seed.json)
CHECKS = [
    ("tests/test_llm_chain.py", False, False),
    ("tests/test_tgbot.py", False, False),
    ("tests/test_admin_env.py", False, False),
    ("tests/e2e_flow.py", True, False),
    ("tests/test_mastery.py", True, False),
    ("tests/test_social.py", True, False),
    ("tests/test_backup.py", True, False),
    ("tests/test_account.py", True, False),
    ("tests/test_slots.py", True, False),
    ("tests/test_chat.py", True, False),
    ("tests/test_chat_commands.py", True, False),
    ("tests/test_chat_moderation.py", True, False),
    ("tests/test_minigames.py", True, False),
    ("tests/test_catalog_admin.py", True, True),
    ("tests/verify_catalog.py", True, True),
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def env_for(tmp: str) -> dict:
    e = dict(os.environ)
    e.update({"LQ_DB": os.path.join(tmp, "test.db"), "DATA_DIR": tmp, "LLM_MOCK": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
              "TELEGRAM_BOT_TOKEN": "", "OPENROUTER_API_KEY": "", "DEEPSEEK_API_KEY": "",
              "ADMIN_USERNAME": "", "ALLOW_REGISTRATION": "1", "CORS_ORIGINS": "https://ashubinov.github.io", "JWT_SECRET": "ci-test-secret", "BACKUP_ENABLED": "0", "BACKUP_KEEP": "3", "SLOTS_MIN_INTERVAL": "0"})
    return e


def run_one(script: str, needs_server: bool) -> bool:
    with tempfile.TemporaryDirectory(prefix="lq-ci-", ignore_cleanup_errors=True) as tmp:
        env, proc, log = env_for(tmp), None, None
        if needs_server:
            env["TELEGRAM_ENABLED"] = "0"  # серверу в тестах бот не нужен; сам тест бота (без сервера) задаёт фейковый токен и API
        args = [PY, str(ROOT / script)]
        try:
            if needs_server:
                port = free_port()
                log = open(os.path.join(tmp, "server.log"), "w", encoding="utf-8")
                proc = subprocess.Popen([PY, "-m", "uvicorn", "server.main:app", "--host", "127.0.0.1", "--port", str(port)],
                                        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
                base = f"http://127.0.0.1:{port}"
                for _ in range(100):
                    try:
                        urllib.request.urlopen(base + "/api/health", timeout=1).read()
                        break
                    except Exception:
                        if proc.poll() is not None:
                            raise RuntimeError("сервер не запустился:\n" + open(os.path.join(tmp, "server.log"), encoding="utf-8").read()[-2000:])
                        time.sleep(0.3)
                else:
                    raise RuntimeError("сервер не ответил за 30 секунд")
                args.append(base)
            r = subprocess.run(args, cwd=ROOT, env=env, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=900)
            ok = r.returncode == 0
            if not ok:
                print(r.stdout[-6000:])
                if proc is not None:
                    log_path = os.path.join(tmp, "server.log")
                    print("--- лог сервера ---\n" + open(log_path, encoding="utf-8", errors="replace").read()[-3000:])
            return ok
        except Exception as e:  # noqa: BLE001
            print("ОШИБКА:", e)
            return False
        finally:
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if log is not None:
                log.close()


def main() -> int:
    t0, failed = time.time(), []
    print("→ проверка синтаксиса")
    if subprocess.run([PY, "-m", "compileall", "-q", str(ROOT / "server"), str(ROOT / "tests"), str(ROOT / "scripts")]).returncode != 0:
        failed.append("compileall")
    for script, needs_server, needs_seed in CHECKS:
        if needs_seed and not HAS_SEED:
            print(f"→ {script}: пропущено (нет catalog_seed.json)")
            continue
        t = time.time()
        ok = run_one(script, needs_server)
        print(f"{'✔' if ok else '✘'} {script} ({time.time() - t:.0f} с)")
        if not ok:
            failed.append(script)
    print(f"\nИтого: {'всё зелёное' if not failed else 'ПРОВАЛЕНО: ' + ', '.join(failed)} ({time.time() - t0:.0f} с)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
