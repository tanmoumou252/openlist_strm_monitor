@echo off
rem WebUI regression suite: JS tests + pytest webui marker
rem Requires Node >= 20.19（与 src/webui/package.json 的 engines 及
rem src/webui/tests/README.md 声明一致；node --test 的最低可用版本即满足）。
rem JS 用例文件由 cmd 侧枚举后以显式路径传参，不依赖 Node 对引号 glob 的
rem 展开语义（旧版本可能不展开且不报错，导致 JS 侧零覆盖仍绿灯）。
rem Zero-match guard: JS_COUNT=0 时显式判红，绝不带零覆盖继续跑 pytest。
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "JS_COUNT=0"
set "JS_ARGS="
for %%F in ("src\webui\tests\*.test.mjs") do (
    set /a JS_COUNT+=1
    set JS_ARGS=!JS_ARGS! "%%~F"
)
if !JS_COUNT! EQU 0 (
    echo [ERROR] src\webui\tests 下无任何 *.test.mjs，JS 侧零覆盖（判红）
    exit /b 1
)
echo [INFO] node:test 收集 %JS_COUNT% 个 JS 用例文件
node.exe --test !JS_ARGS!
if errorlevel 1 exit /b %ERRORLEVEL%
python.exe -m pytest src/tests -m webui
exit /b %ERRORLEVEL%
