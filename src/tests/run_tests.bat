@echo off
rem 运行全部 pytest 测试套件（src/tests/）
rem 用法：run_tests.bat            （默认详细模式）
rem       run_tests.bat --cov      （带覆盖率报告，需 pip install pytest-cov）

setlocal
cd /d "%~dp0"

if "%1"=="--cov" (
    python -m pytest src/tests/ -v --cov=src --cov-report=html
) else (
    python -m pytest src/tests/ -v
)
endlocal
