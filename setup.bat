@echo off
rem One-command setup for Kairos on Windows:  setup.bat [--quick] [--check] [--no-frontend] ...
rem Finds a Python 3.10-3.12 interpreter (via the py launcher) and hands over to scripts\setup.py.
cd /d "%~dp0"
for %%V in (3.11 3.12 3.10) do (
  py -%%V -c "import sys" >nul 2>&1 && (
    py -%%V scripts\setup.py %*
    exit /b %errorlevel%
  )
)
python -c "import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,12) else 1)" >nul 2>&1 && (
  python scripts\setup.py %*
  exit /b %errorlevel%
)
echo Kairos needs Python 3.10, 3.11 or 3.12 (3.13+ is not supported yet). Install 3.11 from https://www.python.org/downloads/
echo or skip Python entirely with Docker:  docker compose up --build
exit /b 1
