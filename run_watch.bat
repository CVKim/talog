@echo off
rem talog watch resident monitor launcher
pushd "%~dp0"
set "EXE="
if exist "%~dp0talog.exe" set "EXE=%~dp0talog.exe"
if not defined EXE if exist "%~dp0dist\talog.exe" set "EXE=%~dp0dist\talog.exe"
if defined EXE ( "%EXE%" watch --config "%~dp0watch.yaml" ) else ( python -m talog watch --config "%~dp0watch.yaml" )
pause
popd