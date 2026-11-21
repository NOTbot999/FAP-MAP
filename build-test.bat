@echo off
rem Builds a TEST version: dist-test\FrancMapTest\FrancMapTest.exe
rem It uses its own data folder (%LOCALAPPDATA%\FrancMapTest) and "Franc Map TEST" in the title,
rem so it never touches the real app's settings, drawings or map cache.
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv
  .venv\Scripts\python -m pip install pywebview requests pyinstaller
)
if not exist build-test mkdir build-test
echo test> build-test\flavor.txt
.venv\Scripts\python tools\make_icon.py
.venv\Scripts\pyinstaller --noconfirm --clean --windowed --name FrancMapTest ^
  --distpath dist-test --workpath build-test\work --specpath build-test ^
  --icon "%~dp0tools\francmap.ico" ^
  --add-data "%~dp0web;web" ^
  --add-data "%~dp0build-test\flavor.txt;." ^
  app.py
echo.
echo Done: dist-test\FrancMapTest\FrancMapTest.exe
