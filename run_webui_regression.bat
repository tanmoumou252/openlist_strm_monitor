@echo off
rem WebUI regression suite: JS tests + pytest webui marker
setlocal
cd /d "%~dp0"
node.exe --test "src/webui/tests/*.test.mjs"
if errorlevel 1 exit /b %ERRORLEVEL%
python.exe -m pytest src/tests -m webui
exit /b %ERRORLEVEL%
