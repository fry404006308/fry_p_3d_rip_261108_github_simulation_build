@echo off
rem fry_gh_publish.bat -- one-click: binaries from the packaging repo -> GitHub multi-platform CI (linux x64/arm64, macOS arm64/intel, windows) -> verdict + deliverables back here.
rem   Usage: double-click (= all) or  fry_gh_publish.bat stage|push|ci|record|release|deliver|status [options]
rem   Needs: git + gh (gh auth login done), this repo set to public while running.
rem   *** PURE ASCII ONLY *** (cmd parses .bat in the OEM codepage)
chcp 65001 >nul
setlocal
set "HERE=%~dp0"
if defined FRY_PY set "PY=%FRY_PY%"
if not defined PY if exist "%HERE%..\fry_py_260604_c180_depth_dither_more\fry_bats\machine_config.bat" call "%HERE%..\fry_py_260604_c180_depth_dither_more\fry_bats\machine_config.bat"
if not defined PY set "PY=python"
if not exist "%HERE%_logs" mkdir "%HERE%_logs"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyMMdd_HHmm"') do set "STAMP=%%i"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "ARGS=%*"
if "%ARGS%"=="" set "ARGS=all"
echo [fry_gh_publish] python = %PY%
echo [fry_gh_publish] log    = %HERE%_logs\_%STAMP%_gh_publish.log
"%PY%" -X utf8 -u "%HERE%tools\fry_gh_publish.py" %ARGS% --result-json "%HERE%_logs\_%STAMP%_result.json" > "%HERE%_logs\_%STAMP%_gh_publish.log" 2>&1
set "RC=%ERRORLEVEL%"
type "%HERE%_logs\_%STAMP%_gh_publish.log"
echo [fry_gh_publish] rc = %RC%
if not "%FRY_NO_PAUSE%"=="1" pause
exit /b %RC%
