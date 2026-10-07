@echo off
chcp 65001 >nul
cd /d "%~dp0"
where cloudflared >nul 2>nul
if errorlevel 1 (
  echo Не найден cloudflared. Установи один раз командой:
  echo     winget install Cloudflare.cloudflared
  echo затем запусти этот файл снова.
  pause
  exit /b 1
)
echo Приложение должно быть запущено (start.bat). Ниже появится публичный адрес вида https://xxxx.trycloudflare.com
echo Он действует, пока открыто это окно. Перед этим создай свои аккаунты и поставь ALLOW_REGISTRATION=0 в .env.
cloudflared tunnel --url http://localhost:8000
pause
