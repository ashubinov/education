@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Собирает/догенерирует готовые курсы из папки "курсы" и сохраняет catalog_seed.json. Можно запускать повторно.
".venv\Scripts\python.exe" scripts\seed_catalog.py
pause
