@echo off
REM Clinical Round - run on your own Windows computer (double-click).
setlocal
cd /d "%~dp0"
where python >nul 2>nul || (echo Python 3.12+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
if not exist ".venv\Scripts\python.exe" (
  echo First run: creating .venv and installing packages...
  python -m venv .venv || (echo Could not create .venv & pause & exit /b 1)
  ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || (echo Package installation failed & pause & exit /b 1)
)
start "" http://127.0.0.1:5000
".venv\Scripts\python.exe" app.py
pause
