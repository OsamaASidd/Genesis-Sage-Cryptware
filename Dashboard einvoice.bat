@echo off
title Nigeria E-Invoicing Dashboard - Genesis Food
color 0A

echo.
echo  ========================================
echo   Nigeria E-Invoicing Dashboard
echo   Genesis Food Nigeria Limited
echo  ========================================
echo.

cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found in PATH.
    echo  Please install Python and try again.
    pause
    exit /b 1
)

if exist venv\Scripts\activate.bat (
    echo  Activating virtual environment...
    call venv\Scripts\activate.bat
) else (
    echo  [WARN] No venv found - installing to system Python...
    pip install flask pyodbc requests reportlab qrcode Pillow --quiet
)

echo.
echo  Starting Genesis Food dashboard at http://[public-ip]:5002
echo  Press Ctrl+C to stop.
echo.

for /f "delims=" %%I in ('powershell -NoProfile -Command "(Invoke-WebRequest -Uri https://api.ipify.org -UseBasicParsing).Content.Trim()"') do set PUBLIC_IP=%%I
start "" cmd /c "timeout /t 3 >nul && start http://%PUBLIC_IP%:5002"

python genesis_app.py

pause