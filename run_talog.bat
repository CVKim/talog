@echo off
rem ============================================================
rem  talog - talos 로그 진단 분석기 실행 배치
rem  사용법 1) 이 파일에 로그 폴더를 드래그&드롭
rem  사용법 2) 더블클릭 후 경로 입력
rem ============================================================
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "LOGS=%~1"
if "%LOGS%"=="" (
    echo.
    echo  [talog] 로그 폴더 경로를 입력하세요.
    echo   - 설비-일자 폴더 ^(예: H:\...\cmfb#1\29^) 또는
    echo   - 설비 폴더     ^(예: H:\...\cmfb#1 - 일자별 리포트 일괄 생성^)
    set /p LOGS="  로그 폴더: "
)
if "!LOGS!"=="" ( echo 경로가 없습니다. & pause & exit /b 1 )

echo.
echo  [talog] 레시피 폴더 ^(예: D:\AIV\MODEL\CMFB - V2^). 없으면 Enter.
set /p RECIPE="  레시피 폴더: "

set "ARGS="!LOGS!" --open"
if not "!RECIPE!"=="" set ARGS=!ARGS! --recipe "!RECIPE!"

echo.
if exist "%~dp0dist\talog.exe" (
    echo  [talog] 실행: talog.exe !ARGS!
    "%~dp0dist\talog.exe" !ARGS!
) else (
    echo  [talog] 실행: python -m talog !ARGS!
    python -m talog !ARGS!
)
echo.
pause
