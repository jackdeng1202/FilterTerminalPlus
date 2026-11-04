@echo off
rem FilterTerminalPlus launcher
rem Path contains no non-ASCII chars, safe for cmd codepage.
set "PYW=C:\Users\jack_deng\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
if exist "%PYW%" goto run
for /f "delims=" %%P in ('where pythonw 2^>nul') do (
    set "PYW=%%P"
    goto run
)
echo [FilterTerminalPlus] pythonw.exe not found.
echo Install Python or fix the path in this file.
pause
exit /b 1
:run
cd /d "%~dp0"
start "FilterTerminalPlus" "%PYW%" filterterminal_plus.py
