#!/bin/bash
# Перезапуск тестового сервера (порт 8011, мок-LLM, отдельная БД). Использование: tests/restart_test_server.sh [clean]
SCR="${SCR:-/tmp}"
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8011 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id \$_.OwningProcess -Force }" >/dev/null 2>&1
sleep 1
[ "$1" = "clean" ] && rm -f "$SCR"/test.db*
(TELEGRAM_BOT_TOKEN= OPENROUTER_API_KEY= DEEPSEEK_API_KEY= CORS_ORIGINS=https://ashubinov.github.io LQ_DB="$SCR/test.db" LLM_MOCK=1 PYTHONUTF8=1 nohup .venv/Scripts/python -m uvicorn server.main:app --port 8011 --host 127.0.0.1 > "$SCR/server.log" 2>&1 &)
sleep 4
curl -s -m 3 http://127.0.0.1:8011/api/health; echo
