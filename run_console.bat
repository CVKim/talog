@echo off
rem talog watch + web console launcher (http://127.0.0.1:8778)
pushd "%~dp0"
set "EXE="
if exist "%~dp0talog.exe" set "EXE=%~dp0talog.exe"
if not defined EXE if exist "%~dp0dist\talog.exe" set "EXE=%~dp0dist\talog.exe"
if defined EXE ( "%EXE%" watch --ui --config "%~dp0watch.yaml" ) else ( python -m talog watch --ui --config "%~dp0watch.yaml" )
pause
popd
