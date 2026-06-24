@echo OFF
setlocal EnableExtensions DisableDelayedExpansion
title OpenList STRM Bridge Debug Console

set "ROOT_DIR=%~dp0"
set "PYTHON_EXE=%ROOT_DIR%python_embed\python.exe"

if exist "%PYTHON_EXE%" (
    "%PYTHON_EXE%" "%ROOT_DIR%src\debug_console.py"
) else (
    python "%ROOT_DIR%src\debug_console.py"
)

exit /b %ERRORLEVEL%