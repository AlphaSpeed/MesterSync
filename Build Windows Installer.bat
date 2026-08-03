@echo off
setlocal
cd /d "%~dp0"

where py.exe >nul 2>nul
if not errorlevel 1 (
    set "MESTERSYNC_PYTHON=py.exe"
) else (
    where python.exe >nul 2>nul
    if errorlevel 1 (
        echo Python 3 was not found. Install Python 3.10 or newer and try again.
        pause
        exit /b 1
    )
    set "MESTERSYNC_PYTHON=python.exe"
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "packaging\Build-Installer.ps1" -PythonCommand "%MESTERSYNC_PYTHON%"
if errorlevel 1 (
    echo.
    echo The installer build did not finish.
    pause
    exit /b 1
)
echo.
echo The installer is in dist\installer.
pause
