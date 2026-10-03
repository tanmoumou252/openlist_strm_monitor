@echo off
rem WebUI regression suite: JS tests + pytest webui marker
rem Requires Node >= 21 (positional args to --test are expanded as globs;
rem verified on v22). Directory-form args are NOT portable on Windows
rem (treated as a module path and fail with MODULE_NOT_FOUND).
rem Zero-match fake-green guard: modern Node reports an unfulfilled glob
rem as a test failure (non-zero exit), which fails this script naturally.
setlocal
cd /d "%~dp0"
node.exe --test "src/webui/tests/*.test.mjs"
if errorlevel 1 exit /b %ERRORLEVEL%
python.exe -m pytest src/tests -m webui
exit /b %ERRORLEVEL%
