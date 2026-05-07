@echo off
setlocal
cd /d "%~dp0\.."
python scripts\build_exe.py
exit /b %ERRORLEVEL%
