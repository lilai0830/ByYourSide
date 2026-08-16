@echo off
REM GOAI AI Glasses Agent - local server launcher
REM Double-click to start. Picks the first free port from 8000/8010/...
REM and runs backend/server.py inside the project's .venv.

cd /d "%~dp0"

REM 1. Ensure the virtual environment exists.
if not exist ".venv\Scripts\python.exe" (
    echo [setup] .venv not found, creating one with: py -m venv .venv
    py -m venv .venv
    if errorlevel 1 (
        echo [error] Failed to create .venv. Make sure the Python launcher py is installed.
        pause
        exit /b 1
    )
    echo [setup] Installing dependencies from requirements.txt ...
    ".venv\Scripts\pip.exe" install -r requirements.txt
    if errorlevel 1 (
        echo [error] Dependency installation failed.
        pause
        exit /b 1
    )
)

REM 2. Pick the first available port from the common list.
set "PORTFILE=%TEMP%\goai_port.tmp"
powershell -NoProfile -Command "$ports=@(8000,8010,8020,8030,8040,8050,8060,8070,8080); foreach($p in $ports){ try{ $l=New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Any,$p); $l.Start(); $l.Stop(); $p; break }catch{} }" > "%PORTFILE%" 2>nul
set "PORT="
if exist "%PORTFILE%" (
    set /p PORT=<"%PORTFILE%"
)
if not defined PORT set "PORT=8000"
if exist "%PORTFILE%" del /f /q "%PORTFILE%" >nul 2>nul

REM 3. Launch the server.
echo [start] Starting GOAI server on port %PORT% ...
echo [start] Open: http://localhost:%PORT%/
echo [start] Press Ctrl+C to stop.
".venv\Scripts\python.exe" backend/server.py %PORT%

echo [stop] Server stopped.
pause
