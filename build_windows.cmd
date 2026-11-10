@echo off
setlocal
cd /d "%~dp0"
echo =========================================
echo Gallery Monitor v80 - PySide6 EXE builder
 echo =========================================
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_windows.ps1"
if errorlevel 1 (
    echo.
    echo [ERROR] Build failed. Please review the messages above.
    pause
    exit /b 1
)
echo.
echo [OK] EXE files are in the dist folder.
pause
endlocal
