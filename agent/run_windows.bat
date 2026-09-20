@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Creating Windows virtual environment...
  py -3 -m venv .venv
  if errorlevel 1 (
    echo Could not create Python virtual environment.
    echo Install Python 3 from python.org and enable the PATH option.
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)
".venv\Scripts\python.exe" vlc_sync_agent.py
