@echo off
rem Builds dist\FrancMap\FrancMap.exe (portable folder — copy it anywhere)
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv
  .venv\Scripts\python -m pip install pywebview requests pyinstaller
)
.venv\Scripts\python tools\make_icon.py
.venv\Scripts\pyinstaller --noconfirm --clean --windowed --name FrancMap ^
  --icon tools\francmap.ico ^
  --add-data "web;web" ^
  app.py
echo.
echo Done: dist\FrancMap\FrancMap.exe
