@echo off
rem ============================================================
rem  talog watch - 예지보전 상주 감시 실행
rem  설정: watch.yaml (감시 경로/룰/알림/LLM)
rem  중지: 이 창을 닫거나 Ctrl+C
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"

if exist "%~dp0dist\talog.exe" (
    "%~dp0dist\talog.exe" watch --config "%~dp0watch.yaml"
) else (
    python -m talog watch --config "%~dp0watch.yaml"
)
pause
