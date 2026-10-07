@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Сначала один раз запусти start.bat, чтобы создалось окружение.
  pause
  exit /b 1
)
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\LearnQuest.lnk'); $s.TargetPath='wscript.exe'; $s.Arguments='\"%~dp0start_hidden.vbs\"'; $s.WorkingDirectory='%~dp0'; $s.Save()"
echo Готово: LearnQuest будет запускаться в фоне при входе в Windows (адрес http://127.0.0.1:8000).
echo Убрать автозапуск: удали ярлык LearnQuest из папки shell:startup.
pause
