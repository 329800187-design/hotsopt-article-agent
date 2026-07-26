@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0Hotspot License Admin.exe" (
  start "" "%~dp0Hotspot License Admin.exe"
  exit /b 0
)
if exist "%~dp0.venv\Scripts\python.exe" (
  start "" "%~dp0.venv\Scripts\python.exe" -m license_admin.license_generator_gui
  exit /b 0
)
echo License signing tool is unavailable. Please use the complete project folder.
pause
exit /b 1
