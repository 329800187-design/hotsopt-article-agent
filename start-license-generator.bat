@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"

set "SIGNER_EXE=%~dp0热点图文工作台_本地许可证签发工具.exe"
if exist "%SIGNER_EXE%" (
  start "" "%SIGNER_EXE%"
  if errorlevel 1 goto module_failed
  exit /b 0
)

set "LEGACY_EXE=%~dp0Hotspot License Admin.exe"
if exist "%LEGACY_EXE%" (
  start "" "%LEGACY_EXE%"
  if errorlevel 1 goto module_failed
  exit /b 0
)

if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" -c "import license_admin.license_generator_gui" >nul 2>&1
  if errorlevel 1 goto module_failed
  start "" "%~dp0.venv\Scripts\python.exe" -m license_admin.license_generator_gui
  if errorlevel 1 goto module_failed
  exit /b 0
)

where py.exe >nul 2>&1
if not errorlevel 1 (
  py -3 -c "import license_admin.license_generator_gui" >nul 2>&1
  if errorlevel 1 goto module_failed
  start "" py -3 -m license_admin.license_generator_gui
  if errorlevel 1 goto module_failed
  exit /b 0
)

where python.exe >nul 2>&1
if not errorlevel 1 (
  python -c "import license_admin.license_generator_gui" >nul 2>&1
  if errorlevel 1 goto module_failed
  start "" python -m license_admin.license_generator_gui
  if errorlevel 1 goto module_failed
  exit /b 0
)

echo [LICENSE_SIGNER_EXE_MISSING] 未找到正式或兼容签发工具 EXE。
echo [PYTHON_ENVIRONMENT_MISSING] 同时未找到可用的 Python 环境。
pause
exit /b 2

:module_failed
echo [LICENSE_SIGNER_MODULE_FAILED] 签发模块启动失败，请检查依赖和项目完整性。
pause
exit /b 3
