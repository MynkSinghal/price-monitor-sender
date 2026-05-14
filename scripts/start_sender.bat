@echo off
REM --------------------------------------------------------------
REM  Price Monitor Sender — launcher (called by Task Scheduler).
REM  Assumes:
REM    * Python 3.13 is on PATH (or update PYTHON_EXE below).
REM    * A venv named .venv exists in the project root.
REM --------------------------------------------------------------
setlocal

set PROJECT_ROOT=%~dp0..
set PYTHON_EXE=%PROJECT_ROOT%\.venv\Scripts\python.exe

if not exist "%PYTHON_EXE%" (
    echo [ERROR] venv not found at %PYTHON_EXE%
    echo.
    echo Run ONE of the following depending on which Python command works on this server:
    echo   python  -m venv .venv  ^&^&  .venv\Scripts\pip install -r requirements.txt
    echo   python3 -m venv .venv  ^&^&  .venv\Scripts\pip install -r requirements.txt
    echo   py      -m venv .venv  ^&^&  .venv\Scripts\pip install -r requirements.txt
    echo.
    echo To check which one works:  python --version  /  python3 --version  /  py --version
    exit /b 2
)

cd /d "%PROJECT_ROOT%"
"%PYTHON_EXE%" -m src.main
exit /b %ERRORLEVEL%
