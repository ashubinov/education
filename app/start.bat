@echo off
chcp 65001 >nul
cd /d "%~dp0"
title LearnQuest

if not exist ".venv\Scripts\python.exe" (
  echo Первый запуск: создаю окружение и ставлю зависимости, подожди минуту...
  python -m venv .venv
  if errorlevel 1 (
    echo Не удалось создать окружение. Установи Python 3.11+ с python.org и поставь галочку "Add to PATH".
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install -q --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
  if errorlevel 1 (
    echo Не удалось установить зависимости. Проверь интернет и запусти снова.
    pause
    exit /b 1
  )
)

if not exist ".env" (
  if exist ".env.example" copy ".env.example" ".env" >nul
)

".venv\Scripts\python.exe" run.py
echo.
echo Сервер остановлен.
pause
