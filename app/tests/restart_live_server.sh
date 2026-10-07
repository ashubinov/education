#!/bin/bash
# Сервер на :8012 с РЕАЛЬНЫМ ключом из .env, но отдельной БД и без Telegram (чтобы не мешать боту).
SCR="${SCR:-/tmp}"
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8012 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id \$_.OwningProcess -Force }" >/dev/null 2>&1
sleep 1
[ "$1" = "clean" ] && rm -f "$SCR"/live.db*
(TELEGRAM_BOT_TOKEN= LQ_DB="$SCR/live.db" PYTHONUTF8=1 nohup .venv/Scripts/python -u -m uvicorn server.main:app --port 8012 --host 127.0.0.1 > "$SCR/live.log" 2>&1 &)
sleep 4
curl -s -m 3 http://127.0.0.1:8012/api/health; echo
