@echo off
setlocal enabledelayedexpansion
rem ------------------------------------------------------------------
rem  talog launcher
rem   1) Just double-click  -- finds newest day folder under
rem                            D:\AIV_LOG\Talos\<YYYY_MM>\<DD>
rem   2) Drag a log folder onto this file
rem   3) Or type any path when asked
rem  ASCII only on purpose: cmd.exe misreads UTF-8 text in batch files.
rem ------------------------------------------------------------------
pushd "%~dp0"
title talog - talos log analyzer

set "ROOT=D:\AIV_LOG\Talos"
set "LOGS=%~1"
if not "%LOGS%"=="" goto have_path
if not exist "%ROOT%\" goto ask_path

echo.
echo  [talog] Scanning %ROOT%
set "N=0"
for /f "delims=" %%M in ('dir /b /ad /o-n "%ROOT%" 2^>nul') do (
    for /f "delims=" %%D in ('dir /b /ad /o-n "%ROOT%\%%M" 2^>nul') do (
        if !N! lss 10 (
            set /a N+=1
            set "P!N!=%ROOT%\%%M\%%D"
            echo     [!N!] %%M\%%D
        )
    )
)
if "!N!"=="0" goto ask_path

echo.
set "SEL="
set /p SEL=  Pick number  ^(Enter = 1 = newest^), or type a full path:
if "!SEL!"=="" set "SEL=1"
set "NOTNUM="
for /f "delims=0123456789" %%X in ("!SEL!") do set "NOTNUM=1"
if defined NOTNUM (
    set "LOGS=!SEL!"
) else (
    call set "LOGS=%%P!SEL!%%"
)
if "!LOGS!"=="" goto ask_path
goto have_path

:ask_path
echo.
echo  [talog] Enter log folder path
echo          example: D:\AIV_LOG\Talos\2026_09\05
set /p LOGS=  LOG DIR:
if "!LOGS!"=="" (
    echo  No path given.
    pause
    popd
    exit /b 1
)

:have_path
rem strip surrounding quotes if the user pasted a quoted path
for /f "tokens=* delims=" %%A in ("!LOGS!") do set "LOGS=%%~A"
if not exist "!LOGS!\" (
    echo.
    echo  [ERROR] Folder not found: !LOGS!
    pause
    popd
    exit /b 1
)

echo.
echo  [talog] Target: !LOGS!
echo.
echo  Recipe folder is optional ^(folder that has ALG.ini^). Enter = skip.
set "RECIPE="
set /p RECIPE=  RECIPE DIR:
if defined RECIPE for /f "tokens=* delims=" %%A in ("!RECIPE!") do set "RECIPE=%%~A"
if defined RECIPE if not exist "!RECIPE!\" (
    echo  [warn] Recipe folder not found - continuing without it.
    set "RECIPE="
)

echo.
echo  Fast mode skips huge dependency-graph logs ^(recommended^).
set "FASTANS="
set /p FASTANS=  Fast mode? [Y/n]:
set "FAST=--fast"
if /i "!FASTANS!"=="n" set "FAST="

set "EXE="
if exist "%~dp0talog.exe" set "EXE=%~dp0talog.exe"
if not defined EXE if exist "%~dp0dist\talog.exe" set "EXE=%~dp0dist\talog.exe"
if defined EXE goto run_exe

python -c "import talog" 2>nul
if errorlevel 1 (
    echo.
    echo  [ERROR] talog.exe not found next to this bat, and no Python talog package.
    echo          Put talog.exe in the same folder as this file.
    pause
    popd
    exit /b 1
)
echo.
if "!RECIPE!"=="" (
    python -m talog "!LOGS!" !FAST! --open
) else (
    python -m talog "!LOGS!" --recipe "!RECIPE!" !FAST! --open
)
goto done

:run_exe
echo.
if "!RECIPE!"=="" (
    "!EXE!" "!LOGS!" !FAST! --open
) else (
    "!EXE!" "!LOGS!" --recipe "!RECIPE!" !FAST! --open
)

:done
echo.
echo  [talog] Report folder: !LOGS!\talog_out
echo.
pause
popd
