@echo off
rem Build SensSuite: dist\SensSuite\ with SensSuite.exe + SensFinder.exe
cd /d "%~dp0.."
py -3 build\make_icon.py || exit /b 1
py -3 -m PyInstaller --noconfirm --clean --distpath dist --workpath build\_work build\SensSuite.spec
