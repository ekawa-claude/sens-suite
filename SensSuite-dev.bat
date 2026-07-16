@echo off
rem Dev launcher (needs Python + deps). Friends use dist\SensSuite\SensSuite.exe.
cd /d "%~dp0studio"
start "" pythonw studio.py
