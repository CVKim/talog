@echo off
rem ------------------------------------------------------------------
rem  talog - AI vision log operations platform
rem   Double-click         : open the console (monitor, incidents,
rem                          analysis, settings) - http://127.0.0.1:8778
rem   Drag a log folder on : build a diagnosis report for that folder
rem  Settings: talog.yaml next to this file (edit in the console).
rem  ASCII only on purpose: cmd.exe misreads UTF-8 text in batch files.
rem ------------------------------------------------------------------
pushd "%~dp0"
title talog
set "EXE="
if exist "%~dp0talog.exe" set "EXE=%~dp0talog.exe"
if not defined EXE if exist "%~dp0dist\talog.exe" set "EXE=%~dp0dist\talog.exe"
set "CFG=%~dp0talog.yaml"
if not exist "%CFG%" if exist "%~dp0watch.yaml" set "CFG=%~dp0watch.yaml"

if "%~1"=="" goto console
echo  [talog] Report for: %~1
if defined EXE ( "%EXE%" analyze "%~1" --fast --open ) else ( python -m talog analyze "%~1" --fast --open )
echo.
pause
popd
exit /b

:console
echo  [talog] Console: http://127.0.0.1:8778  (closing this window stops monitoring)
if defined EXE ( "%EXE%" --config "%CFG%" ) else ( python -m talog --config "%CFG%" )
pause
popd
