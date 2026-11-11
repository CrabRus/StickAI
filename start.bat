@echo off
rem Start StickAI with the project's virtual environment.
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" main.py %*
) else (
  echo No .venv found. First run:
  echo   python -m venv .venv
  echo   .venv\Scripts\pip install -r requirements.txt
  pause
)
